from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

from .core import (
    ACTIVE_STATUSES,
    PHASES,
    elapsed_ms,
    next_break_phase,
    parse_timestamp_ms,
    project_auto_start_breaks,
    project_durations,
    rebuild_tasks,
    rebuild_optimistic,
    reduce_command,
    task_from_title,
)
from .uuid7 import reserve_uuid7, uuid7_parts
from .secure_store import PlatformSecretStore

DURATION_MIN_MS = 60_000
PREFERENCE_DURATION_MAX_MS = 10_800_000
CANONICAL_DURATION_MAX_MS = 14_400_000
RESOLUTION_OPERATION_MAX = 4_096
ACKNOWLEDGEMENT_OUTCOMES = {"applied", "ignored", "rejected"}
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CLOCK_SKEW_MS = 300_000
MAX_SERVER_TIME_UNCERTAINTY_MS = 30_000
MAX_CLOCK_CONTINUITY_DRIFT_MS = 1_000
COMMAND_TYPES = {"start", "pause", "resume", "finish", "cancel", "clear"}


def default_data_path() -> Path:
    return user_data_path("pomodorough", appauthor=False) / "pomodorough.sqlite3"


def utc_timestamp(milliseconds: int) -> str:
    value = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(
        self,
        path: Path | None = None,
        *,
        iroh_secret_store: PlatformSecretStore | None = None,
    ) -> None:
        self._trusted_time_anchor: dict[str, int] | None = None
        self._timer_time_anchor: dict[str, Any] | None = None
        self.path = path or default_data_path()
        self._iroh_secret_store = iroh_secret_store or PlatformSecretStore()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_commands (
                id TEXT PRIMARY KEY,
                device_sequence INTEGER NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                depends_on_command_id TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_task_operations (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_duration_operations (
                id TEXT PRIMARY KEY,
                phase TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_auto_start_operations (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_selected_task_operations (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_auto_breaks (
                finish_command_id TEXT PRIMARY KEY,
                timer_id TEXT NOT NULL,
                finish_device_sequence INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_auto_break_starts (
                source_finish_command_id TEXT PRIMARY KEY,
                source_timer_id TEXT NOT NULL,
                start_command_id TEXT NOT NULL UNIQUE,
                selected_phase_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pending_phase_advances (
                finish_command_id TEXT PRIMARY KEY,
                timer_id TEXT NOT NULL,
                source_phase TEXT NOT NULL,
                advanced_phase TEXT NOT NULL,
                selected_phase_version INTEGER NOT NULL
            );
            """
        )
        self.connection.commit()
        with self._immediate_transaction():
            command_columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(pending_commands)")
            }
            if "depends_on_command_id" not in command_columns:
                self.connection.execute(
                    "ALTER TABLE pending_commands ADD COLUMN depends_on_command_id TEXT"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS pending_commands_dependency "
                "ON pending_commands(depends_on_command_id)"
            )
            auto_break_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(pending_auto_break_starts)"
                )
            }
            if "selected_phase_version" not in auto_break_columns:
                self.connection.execute(
                    "ALTER TABLE pending_auto_break_starts ADD COLUMN "
                    "selected_phase_version INTEGER NOT NULL DEFAULT 0"
                )
            for statement in (
                """CREATE TABLE IF NOT EXISTS iroh_rooms (
                    room_id TEXT PRIMARY KEY,
                    room_secret BLOB NOT NULL,
                    room_name TEXT,
                    return_workspace TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    conflict TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS iroh_records (
                    room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
                    domain TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    record TEXT NOT NULL,
                    PRIMARY KEY(room_id, domain, operation_id)
                )""",
                """CREATE TABLE IF NOT EXISTS iroh_peers (
                    room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
                    endpoint_id TEXT NOT NULL,
                    endpoint_ticket TEXT NOT NULL,
                    device_id TEXT,
                    display_name TEXT,
                    last_seen_at_ms INTEGER,
                    PRIMARY KEY(room_id, endpoint_id)
                )""",
                """CREATE TABLE IF NOT EXISTS iroh_conflicts (
                    room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
                    domain TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    local_digest TEXT NOT NULL,
                    received_digest TEXT NOT NULL,
                    received_record TEXT,
                    detected_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(room_id, domain, operation_id, received_digest)
                )""",
                """CREATE INDEX IF NOT EXISTS iroh_records_inventory
                    ON iroh_records(room_id, domain, operation_id)""",
                """CREATE INDEX IF NOT EXISTS iroh_peers_recent
                    ON iroh_peers(room_id, last_seen_at_ms DESC)""",
            ):
                self.connection.execute(statement)
            self._migrated_iroh_capabilities = self._migrate_plaintext_iroh_capabilities()
            self._set_meta("irohSchemaVersion", 1)
        if self._migrated_iroh_capabilities:
            self.connection.execute("VACUUM")
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._initialize()
        self._restore_trusted_time_anchor()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _room_secret_key(room_id: str) -> str:
        return f"room-secret:{room_id}"

    @staticmethod
    def _peer_ticket_key(room_id: str, endpoint_id: str) -> str:
        return f"peer-ticket:{room_id}:{endpoint_id}"

    @staticmethod
    def _secure_reference(key: str) -> bytes:
        return f"secure:{key}".encode("utf-8")

    def _migrate_plaintext_iroh_capabilities(self) -> bool:
        migrated = False
        for row in self.connection.execute(
            "SELECT room_id, room_secret FROM iroh_rooms"
        ).fetchall():
            secret = row["room_secret"]
            if not isinstance(secret, bytes) or len(secret) != 32:
                continue
            room_id = str(row["room_id"])
            key = self._room_secret_key(room_id)
            self._iroh_secret_store.save(key, bytes(secret))
            self.connection.execute(
                "UPDATE iroh_rooms SET room_secret = ? WHERE room_id = ?",
                (self._secure_reference(key), room_id),
            )
            migrated = True
        for row in self.connection.execute(
            "SELECT room_id, endpoint_id, endpoint_ticket FROM iroh_peers"
        ).fetchall():
            ticket = row["endpoint_ticket"]
            if not isinstance(ticket, str) or ticket.startswith("secure:"):
                continue
            room_id = str(row["room_id"])
            endpoint_id = str(row["endpoint_id"])
            key = self._peer_ticket_key(room_id, endpoint_id)
            self._iroh_secret_store.save(key, ticket.encode("utf-8"))
            self.connection.execute(
                "UPDATE iroh_peers SET endpoint_ticket = ? "
                "WHERE room_id = ? AND endpoint_id = ?",
                (f"secure:{key}", room_id, endpoint_id),
            )
            migrated = True
        if migrated:
            self.connection.execute("PRAGMA secure_delete=ON")
        return migrated

    def _initialize(self) -> None:
        defaults: dict[str, Any] = {
            "deviceId": f"desktop-{uuid.uuid4()}",
            "deviceSequence": 0,
            "hlc": {"wallMs": 0, "counter": 0},
            "lastUuidV7": None,
            "serverClockSample": None,
            "commandPhysicalTimes": {},
            "selectedPhaseVersion": 0,
            "settings": {
                "selectedPhase": "focus",
                "durations": {
                    phase: definition["default_minutes"] for phase, definition in PHASES.items()
                },
                "durationsMs": {
                    phase: definition["default_minutes"] * 60_000
                    for phase, definition in PHASES.items()
                },
                "autoStartBreaks": False,
                "selectedTaskId": None,
            },
            "snapshot": {
                "revision": 0,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "knownTasks": [],
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "user": None,
            },
            "pendingSync": None,
            "pendingResolution": None,
            "replicationMode": "centralized",
            "activeIrohRoomId": None,
        }
        with self._immediate_transaction():
            for key, value in defaults.items():
                self.connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                    (key, json.dumps(value, separators=(",", ":"))),
                )
            settings = self.get_meta("settings", {})
            normalized_settings = self._normalize_settings(settings)
            if normalized_settings != settings:
                settings = normalized_settings
                self._set_meta("settings", settings)
            if not self.get_meta("durationMigrationComplete", False):
                for phase, definition in PHASES.items():
                    duration_ms = int(settings["durationsMs"][phase])
                    if duration_ms != definition["default_minutes"] * 60_000:
                        self._queue_duration_operation(
                            phase, duration_ms, settings, 0, bootstrap=True
                        )
                self._set_meta("durationMigrationComplete", True)
            snapshot = self.get_meta("snapshot", {})
            snapshot_had_auto_start = "autoStartBreaks" in snapshot
            changed = False
            for key, value in (
                ("tasks", []),
                ("knownTasks", []),
                ("autoStartBreaks", False),
                ("selectedTaskId", None),
            ):
                if key not in snapshot:
                    snapshot[key] = value
                    changed = True
            if changed:
                self._set_meta("snapshot", snapshot)
            if not self.get_meta("autoStartMigrationComplete", False):
                if settings["autoStartBreaks"]:
                    self._queue_auto_start_operation(
                        True, settings, 0, bootstrap=True
                    )
                self._set_meta(
                    "autoStartLegacyDefaultUnknown",
                    not settings["autoStartBreaks"]
                    and not snapshot_had_auto_start
                    and not self._pending_auto_start_operations(),
                )
                self._set_meta("autoStartMigrationComplete", True)
            if not self.get_meta("selectedTaskMigrationComplete", False):
                selected_task_id = settings.get("selectedTaskId")
                if selected_task_id is None:
                    self._set_meta("selectedTaskMigrationComplete", True)
                elif (
                    self.get_meta("pendingResolution") is None
                    and self.get_meta("replicationMode", "centralized") != "iroh"
                ):
                    self._queue_selected_task_operation(
                        selected_task_id, settings, 0, bootstrap=True
                    )
                    self._set_meta("selectedTaskMigrationComplete", True)
            pending_auto_starts = self._pending_auto_start_operations()
            projected = project_auto_start_breaks(
                bool(snapshot["autoStartBreaks"]), pending_auto_starts
            )
            if settings["autoStartBreaks"] != projected:
                settings["autoStartBreaks"] = projected
                self._set_meta("settings", settings)
            physical_times = self.get_meta("commandPhysicalTimes", {})
            if not isinstance(physical_times, dict):
                physical_times = {}
            pending_ids = set()
            for row in self.connection.execute(
                "SELECT id, payload FROM pending_commands"
            ):
                command_id = str(row["id"])
                pending_ids.add(command_id)
                if command_id in physical_times:
                    continue
                try:
                    command = json.loads(row["payload"])
                    occurred_ms = parse_timestamp_ms(command.get("occurredAt"))
                except (AttributeError, TypeError, json.JSONDecodeError):
                    occurred_ms = None
                if occurred_ms is not None:
                    physical_times[command_id] = occurred_ms
            physical_times = {
                command_id: value
                for command_id, value in physical_times.items()
                if command_id in pending_ids
            }
            self._set_meta("commandPhysicalTimes", physical_times)

    @staticmethod
    def _normalize_settings(settings: Any) -> dict[str, Any]:
        normalized = dict(settings) if isinstance(settings, dict) else {}
        durations = normalized.get("durations")
        durations = durations if isinstance(durations, dict) else {}
        durations_ms = normalized.get("durationsMs")
        durations_ms = durations_ms if isinstance(durations_ms, dict) else {}
        normalized_durations_ms: dict[str, int] = {}
        for phase, definition in PHASES.items():
            default_minutes = int(definition["default_minutes"])
            value = durations_ms.get(phase)
            try:
                duration_ms = int(value) if not isinstance(value, bool) else 0
            except (TypeError, ValueError):
                duration_ms = 0
            if (
                not DURATION_MIN_MS
                <= duration_ms
                <= PREFERENCE_DURATION_MAX_MS
                or duration_ms % 60_000
            ):
                legacy_value = durations.get(phase, default_minutes)
                try:
                    minutes = (
                        int(legacy_value) if not isinstance(legacy_value, bool) else 0
                    )
                except (TypeError, ValueError):
                    minutes = 0
                duration_ms = (
                    minutes * 60_000
                    if 1 <= minutes <= 180
                    else default_minutes * 60_000
                )
            normalized_durations_ms[phase] = duration_ms
        normalized["durationsMs"] = normalized_durations_ms
        normalized["durations"] = {
            phase: Store._display_minutes(duration_ms)
            for phase, duration_ms in normalized_durations_ms.items()
        }
        if normalized.get("selectedPhase") not in PHASES:
            normalized["selectedPhase"] = "focus"
        normalized["autoStartBreaks"] = bool(
            normalized.get("autoStartBreaks", False)
        )
        normalized.setdefault("selectedTaskId", None)
        return normalized

    @staticmethod
    def _display_minutes(duration_ms: int) -> int:
        return duration_ms // 60_000

    @staticmethod
    def _duration_ms(
        value: Any, *, maximum: int = PREFERENCE_DURATION_MAX_MS
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Duration must be an integer number of milliseconds.")
        if not DURATION_MIN_MS <= value <= maximum:
            raise ValueError(
                f"Duration must be between {DURATION_MIN_MS} and {maximum} milliseconds."
            )
        if value % 60_000:
            raise ValueError("Duration must be measured in whole minutes.")
        return value

    @staticmethod
    def _bounded_integer(
        value: Any, label: str, *, minimum: int = 0
    ) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= MAX_SAFE_INTEGER
        ):
            raise ValueError(f"{label} is outside the safe integer range.")
        return value

    @classmethod
    def _physical_time_ms(cls, value: Any) -> int:
        return cls._bounded_integer(value, "Physical occurrence time", minimum=1)

    @staticmethod
    def _signed_safe_integer(value: Any, label: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
        ):
            raise ValueError(f"{label} is outside the safe integer range.")
        return value

    @classmethod
    def _server_clock_sample(cls, value: Any) -> dict[str, int] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Persisted server clock sample is invalid.")
        uncertainty_ms = cls._bounded_integer(
            value.get("uncertaintyMs"), "Persisted server clock uncertainty"
        )
        if uncertainty_ms > MAX_SERVER_TIME_UNCERTAINTY_MS:
            raise ValueError("Persisted server clock uncertainty is too large.")
        if set(value) != {
            "offsetMs",
            "uncertaintyMs",
            "acquiredPhysicalMs",
            "acquiredMonotonicMs",
            "acquiredTrustedMs",
        }:
            raise ValueError("Persisted server clock sample is invalid.")
        offset_ms = cls._signed_safe_integer(
            value.get("offsetMs"), "Persisted server clock offset"
        )
        acquired_physical_ms = cls._physical_time_ms(
            value.get("acquiredPhysicalMs")
        )
        acquired_monotonic_ms = cls._bounded_integer(
            value.get("acquiredMonotonicMs"),
            "Persisted server clock monotonic acquisition",
        )
        acquired_trusted_ms = cls._physical_time_ms(
            value.get("acquiredTrustedMs")
        )
        return {
            "offsetMs": offset_ms,
            "uncertaintyMs": uncertainty_ms,
            "acquiredPhysicalMs": acquired_physical_ms,
            "acquiredMonotonicMs": acquired_monotonic_ms,
            "acquiredTrustedMs": acquired_trusted_ms,
        }

    @classmethod
    def _projected_trusted_time(
        cls,
        sample: dict[str, int],
        physical_ms: int,
        monotonic_ms: int,
    ) -> int | None:
        physical_ms = cls._physical_time_ms(physical_ms)
        monotonic_ms = cls._bounded_integer(monotonic_ms, "Monotonic clock")
        if monotonic_ms < sample["acquiredMonotonicMs"]:
            return None
        elapsed_ms = monotonic_ms - sample["acquiredMonotonicMs"]
        expected_physical_ms = sample["acquiredPhysicalMs"] + elapsed_ms
        if abs(physical_ms - expected_physical_ms) > MAX_CLOCK_CONTINUITY_DRIFT_MS:
            return None
        return cls._physical_time_ms(sample["acquiredTrustedMs"] + elapsed_ms)

    def _restore_trusted_time_anchor(self) -> None:
        with self._immediate_transaction():
            try:
                sample = self._server_clock_sample(
                    self.get_meta("serverClockSample")
                )
            except ValueError:
                self._set_meta("serverClockSample", None)
                return
            if sample is None:
                return
            physical_ms = self._physical_time_ms(int(time.time() * 1000))
            monotonic_ms = self._bounded_integer(
                time.monotonic_ns() // 1_000_000, "Monotonic clock"
            )
            if (
                self._projected_trusted_time(
                    sample, physical_ms, monotonic_ms
                )
                is None
            ):
                self._set_meta("serverClockSample", None)
                return
            self._trusted_time_anchor = sample

    def _clock_sample_for_response(
        self,
        server_time_ms: int,
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None]:
        timings = (
            request_physical_ms,
            received_physical_ms,
            request_monotonic_ms,
            received_monotonic_ms,
        )
        if all(value is None for value in timings):
            return None, None
        if any(value is None for value in timings):
            raise ValueError("Local response timing is incomplete.")

        server_time_ms = self._physical_time_ms(server_time_ms)
        request_physical_ms = self._physical_time_ms(timings[0])
        received_physical_ms = self._physical_time_ms(timings[1])
        request_monotonic_ms = self._bounded_integer(
            timings[2], "Request monotonic clock"
        )
        received_monotonic_ms = self._bounded_integer(
            timings[3], "Response monotonic clock"
        )
        if received_monotonic_ms < request_monotonic_ms:
            raise ValueError("Local response timing is invalid.")
        round_trip_ms = received_monotonic_ms - request_monotonic_ms
        physical_round_trip_ms = received_physical_ms - request_physical_ms
        clock_disagreement_ms = abs(physical_round_trip_ms - round_trip_ms)
        if (
            physical_round_trip_ms < 0
            or clock_disagreement_ms > MAX_CLOCK_CONTINUITY_DRIFT_MS
        ):
            raise ValueError("Local response clocks disagree.")
        midpoint_elapsed_ms = round_trip_ms // 2
        uncertainty_ms = (round_trip_ms + 1) // 2 + clock_disagreement_ms
        if uncertainty_ms > MAX_SERVER_TIME_UNCERTAINTY_MS:
            raise ValueError("Server time sample uncertainty is too large.")
        midpoint_ms = self._physical_time_ms(
            request_physical_ms + midpoint_elapsed_ms
        )
        offset_ms = self._signed_safe_integer(
            server_time_ms - midpoint_ms, "Server clock offset"
        )
        acquired_trusted_ms = self._physical_time_ms(
            server_time_ms + round_trip_ms - midpoint_elapsed_ms
        )
        sample = {
            "offsetMs": offset_ms,
            "uncertaintyMs": uncertainty_ms,
            "acquiredPhysicalMs": received_physical_ms,
            "acquiredMonotonicMs": received_monotonic_ms,
            "acquiredTrustedMs": acquired_trusted_ms,
        }
        return sample, sample

    def _set_trusted_time_anchor(self, anchor: dict[str, int]) -> None:
        self._trusted_time_anchor = dict(anchor)

    @staticmethod
    def _timer_fingerprint(timer: dict[str, Any]) -> tuple[Any, ...]:
        intent = timer.get("lastIntent")
        return (
            timer.get("id"),
            timer.get("status"),
            timer.get("phase"),
            timer.get("plannedDurationMs"),
            timer.get("anchorAt"),
            timer.get("elapsedAtAnchorMs"),
            timer.get("taskId"),
            intent.get("commandId") if isinstance(intent, dict) else None,
        )

    @staticmethod
    def _timer_continuity_identity(timer: dict[str, Any]) -> tuple[Any, ...]:
        intent = timer.get("lastIntent")
        return (
            timer.get("id"),
            timer.get("status"),
            timer.get("phase"),
            timer.get("plannedDurationMs"),
            timer.get("elapsedAtAnchorMs"),
            timer.get("taskId"),
            intent.get("commandId") if isinstance(intent, dict) else None,
        )

    def effective_timer_now_ms(
        self,
        timer: dict[str, Any] | None,
        *,
        physical_ms: int | None = None,
        monotonic_ms: int | None = None,
    ) -> int:
        physical_ms = self._physical_time_ms(
            int(time.time() * 1000) if physical_ms is None else physical_ms
        )
        if not timer or timer.get("status") != "running":
            self._timer_time_anchor = None
            return physical_ms
        monotonic_ms = self._bounded_integer(
            time.monotonic_ns() // 1_000_000
            if monotonic_ms is None
            else monotonic_ms,
            "Monotonic clock",
        )
        identity = self._timer_continuity_identity(timer)
        timer_anchor = self._timer_time_anchor
        if (
            timer_anchor is None
            or timer_anchor["identity"] != identity
            or monotonic_ms < timer_anchor["monotonicMs"]
        ):
            timer_anchor = {
                "identity": identity,
                "elapsedMs": elapsed_ms(timer, physical_ms),
                "monotonicMs": monotonic_ms,
            }
            self._timer_time_anchor = timer_anchor
        projected_elapsed_ms = min(
            int(timer["plannedDurationMs"]),
            timer_anchor["elapsedMs"]
            + monotonic_ms
            - timer_anchor["monotonicMs"],
        )
        anchor_ms = parse_timestamp_ms(timer.get("anchorAt"))
        if anchor_ms is None:
            return physical_ms
        return self._physical_time_ms(
            anchor_ms
            + projected_elapsed_ms
            - int(timer.get("elapsedAtAnchorMs") or 0)
        )

    def _trusted_now_ms(
        self,
        physical_ms: int | None = None,
        *,
        use_server_clock: bool = True,
        use_monotonic: bool = True,
        sample: dict[str, int] | None = None,
    ) -> int:
        physical_ms = self._physical_time_ms(
            int(time.time() * 1000) if physical_ms is None else physical_ms
        )
        if not use_server_clock:
            return physical_ms
        sample = self._server_clock_sample(
            self.get_meta("serverClockSample") if sample is None else sample
        )
        if sample is None:
            return physical_ms
        if not use_monotonic:
            return self._physical_time_ms(physical_ms + sample["offsetMs"])
        monotonic_ms = self._bounded_integer(
            time.monotonic_ns() // 1_000_000, "Monotonic clock"
        )
        anchor = self._trusted_time_anchor
        if (
            anchor is None
            or anchor["offsetMs"] != sample["offsetMs"]
            or anchor["uncertaintyMs"] != sample["uncertaintyMs"]
            or anchor["acquiredPhysicalMs"] != sample["acquiredPhysicalMs"]
            or anchor["acquiredMonotonicMs"] != sample["acquiredMonotonicMs"]
            or anchor["acquiredTrustedMs"] != sample["acquiredTrustedMs"]
        ):
            projected_ms = self._projected_trusted_time(
                sample, physical_ms, monotonic_ms
            )
            if projected_ms is None:
                return physical_ms
            self._set_trusted_time_anchor(sample)
            return projected_ms
        if monotonic_ms < anchor["acquiredMonotonicMs"]:
            return physical_ms
        return self._physical_time_ms(
            anchor["acquiredTrustedMs"]
            + monotonic_ms
            - anchor["acquiredMonotonicMs"]
        )

    @classmethod
    def _logical_clock(
        cls, value: Any, *, allow_legacy_zero: bool = False
    ) -> tuple[int, int]:
        if not isinstance(value, dict):
            raise ValueError("Persisted logical clock is invalid.")
        wall_ms = cls._bounded_integer(
            value.get("wallMs"), "Logical clock wall time"
        )
        counter = cls._bounded_integer(
            value.get("counter"), "Logical clock counter"
        )
        if wall_ms == 0 and (not allow_legacy_zero or counter != 0):
            raise ValueError("Logical clock wall time must be positive.")
        return wall_ms, counter

    @classmethod
    def _operation_clock(
        cls,
        operation: dict[str, Any],
        *,
        allow_legacy_zero: bool = False,
    ) -> tuple[int, int, int]:
        occurred_at = operation.get("occurredAt")
        occurred_ms = (
            parse_timestamp_ms(occurred_at) if isinstance(occurred_at, str) else None
        )
        if occurred_ms is None:
            raise ValueError("Pending operation occurrence time is invalid.")
        wall_ms, counter = cls._logical_clock(
            {
                "wallMs": operation.get("hlcWallMs"),
                "counter": operation.get("hlcCounter"),
            },
            allow_legacy_zero=allow_legacy_zero,
        )
        if allow_legacy_zero and (wall_ms, counter) == (0, 0):
            if occurred_ms != 0:
                raise ValueError("Legacy pending operation clock is invalid.")
            return occurred_ms, wall_ms, counter
        cls._physical_time_ms(occurred_ms)
        if abs(wall_ms - occurred_ms) > MAX_CLOCK_SKEW_MS:
            raise ValueError("Pending operation clock exceeds the trusted-time limit.")
        return occurred_ms, wall_ms, counter

    def _reserve_generation(
        self,
        physical_now_ms: int,
        *,
        sequence_count: int = 0,
        clock_count: int = 1,
        use_server_clock: bool = True,
        use_monotonic: bool = False,
    ) -> tuple[int, list[int], list[tuple[int, int]]]:
        now_ms = self._trusted_now_ms(
            physical_now_ms,
            use_server_clock=use_server_clock,
            use_monotonic=use_monotonic,
        )
        sequence = self._bounded_integer(
            self.get_meta("deviceSequence", 0), "Persisted device sequence"
        )
        old_wall_ms, old_counter = self._logical_clock(
            self.get_meta("hlc", {"wallMs": 0, "counter": 0}),
            allow_legacy_zero=True,
        )
        if sequence_count < 0 or clock_count < 0:
            raise ValueError("Generation reservation is invalid.")
        if sequence_count > MAX_SAFE_INTEGER - sequence:
            raise ValueError("Device sequence has no safe integer headroom.")

        wall_ms = max(now_ms, old_wall_ms)
        if wall_ms - now_ms > MAX_CLOCK_SKEW_MS:
            raise ValueError("Persisted logical clock exceeds the trusted-time limit.")
        first_counter = old_counter + 1 if wall_ms == old_wall_ms else 0
        if clock_count and first_counter > MAX_SAFE_INTEGER - (clock_count - 1):
            raise ValueError("Logical clock counter has no safe integer headroom.")

        sequences = [sequence + offset for offset in range(1, sequence_count + 1)]
        clocks = [
            (wall_ms, first_counter + offset) for offset in range(clock_count)
        ]
        return now_ms, sequences, clocks

    def _pending_uuid7_ids(self) -> list[str]:
        identifiers: list[str] = []
        rows = self.connection.execute(
            "SELECT id FROM pending_commands "
            "UNION ALL SELECT id FROM pending_task_operations "
            "UNION ALL SELECT id FROM pending_duration_operations "
            "UNION ALL SELECT id FROM pending_auto_start_operations "
            "UNION ALL SELECT id FROM pending_selected_task_operations"
        )
        for row in rows:
            identifier = str(row["id"])
            try:
                uuid7_parts(identifier)
            except ValueError:
                continue
            identifiers.append(identifier)
        return identifiers

    def _reserve_uuid7_ids(self, wall_ms: int, count: int) -> list[str]:
        stored = self.get_meta("lastUuidV7")
        if stored is not None:
            uuid7_parts(stored)

        pending = self._pending_uuid7_ids()
        latest_pending = max(
            pending, key=lambda value: uuid.UUID(value).int, default=None
        )
        if stored is None:
            previous = latest_pending
        else:
            previous = str(stored)
            if (
                latest_pending is not None
                and uuid.UUID(latest_pending).int > uuid.UUID(previous).int
            ):
                raise ValueError(
                    "Persisted UUIDv7 state predates a pending identifier."
                )

        identifiers = reserve_uuid7(wall_ms, count, previous)
        self._set_meta("lastUuidV7", identifiers[-1])
        return identifiers

    @classmethod
    def _canonical_durations(cls, durations_ms: Any) -> dict[str, int]:
        if not isinstance(durations_ms, dict) or set(durations_ms) != set(PHASES):
            raise ValueError("Server returned invalid duration preferences.")
        return {phase: cls._duration_ms(durations_ms[phase]) for phase in PHASES}

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def set_meta(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            self._set_meta(key, value)

    def _set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    @property
    def device_id(self) -> str:
        return str(self.get_meta("deviceId"))

    def owns_timer(self, timer: dict[str, Any] | None) -> bool:
        if not isinstance(timer, dict) or not timer.get("id"):
            return False
        if self.replication_mode == "iroh":
            return timer.get("startedByDeviceId") == self.device_id
        ownership = self.get_meta("centralizedTimerOwnership")
        return (
            isinstance(ownership, dict)
            and ownership.get("timerId") == timer["id"]
            and ownership.get("deviceId") == self.device_id
        )

    def load(self, *, projection: bool = False) -> dict[str, Any]:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            settings = self.get_meta("settings")
            snapshot = self.get_meta("snapshot")
            pending = self._pending_commands()
            pending_tasks = [
                json.loads(row["payload"])
                for row in self.connection.execute(
                    "SELECT payload FROM pending_task_operations ORDER BY rowid"
                )
            ]
            pending_durations = [
                json.loads(row["payload"])
                for row in self.connection.execute(
                    "SELECT payload FROM pending_duration_operations ORDER BY rowid"
                )
            ]
            pending_durations.sort(
                key=lambda operation: (
                    int(operation.get("hlcWallMs", 0)),
                    int(operation.get("hlcCounter", 0)),
                    str(operation.get("id", "")),
                )
            )
            pending_auto_starts = self._pending_auto_start_operations()
            pending_selected_tasks = self._pending_selected_task_operations()
            try:
                pending_resolution = self.get_meta("pendingResolution")
            except (TypeError, json.JSONDecodeError):
                pending_resolution = {"corrupted": True}
            state = {
                "settings": settings,
                "snapshot": snapshot,
                "pending": pending,
                "pendingTasks": pending_tasks,
                "pendingDurations": pending_durations,
                "pendingAutoStarts": pending_auto_starts,
                "pendingSelectedTasks": pending_selected_tasks,
                "pendingResolution": pending_resolution,
            }
            if projection:
                state["projectionSnapshot"] = self._physical_snapshot(snapshot)
                state["projectionPending"] = self._physical_pending_commands(pending)
        except BaseException:
            if owns_transaction:
                self.connection.rollback()
            raise
        if owns_transaction:
            self.connection.commit()
        return state

    def _pending_commands(self, *, sendable_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE depends_on_command_id IS NULL" if sendable_only else ""
        return [
            json.loads(row["payload"])
            for row in self.connection.execute(
                f"SELECT payload FROM pending_commands {where} "
                "ORDER BY device_sequence"
            )
        ]

    def _command_physical_times(self) -> dict[str, int]:
        value = self.get_meta("commandPhysicalTimes", {})
        if not isinstance(value, dict):
            raise ValueError("Persisted command physical times are invalid.")
        return {
            command_id: self._physical_time_ms(physical_ms)
            for command_id, physical_ms in value.items()
            if isinstance(command_id, str) and command_id
        }

    def _physical_pending_commands(
        self, commands: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        physical_times = self._command_physical_times()
        projected = []
        for command in commands:
            physical_ms = physical_times.get(str(command.get("id", "")))
            if physical_ms is None:
                projected.append(command)
                continue
            local_command = dict(command)
            local_command["occurredAt"] = utc_timestamp(physical_ms)
            projected.append(local_command)
        return projected

    def _physical_timestamp(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        sample = self._server_clock_sample(self.get_meta("serverClockSample"))
        if sample is None:
            return value
        trusted_ms = parse_timestamp_ms(value)
        if trusted_ms is None:
            return value
        physical_ms = trusted_ms - sample["offsetMs"]
        try:
            return utc_timestamp(self._physical_time_ms(physical_ms))
        except (OSError, OverflowError, ValueError):
            return value

    def _physical_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        projected = deepcopy(snapshot)
        timer = projected.get("canonicalTimer")
        if isinstance(timer, dict):
            timer["anchorAt"] = self._physical_timestamp(timer.get("anchorAt"))
            intent = timer.get("lastIntent")
            if isinstance(intent, dict):
                intent["occurredAt"] = self._physical_timestamp(
                    intent.get("occurredAt")
                )
        for item in projected.get("history", []):
            if not isinstance(item, dict):
                continue
            for key in ("completedAt", "endedAt"):
                if key in item:
                    item[key] = self._physical_timestamp(item.get(key))
        return projected

    def _record_command_physical_time(
        self, command_id: str, physical_ms: int
    ) -> None:
        physical_times = self._command_physical_times()
        physical_times[command_id] = self._physical_time_ms(physical_ms)
        self._set_meta("commandPhysicalTimes", physical_times)

    def _prune_command_physical_times(self) -> None:
        pending_ids = {
            str(row["id"])
            for row in self.connection.execute("SELECT id FROM pending_commands")
        }
        physical_times = self._command_physical_times()
        retained = {
            command_id: physical_ms
            for command_id, physical_ms in physical_times.items()
            if command_id in pending_ids
        }
        if retained != physical_times:
            self._set_meta("commandPhysicalTimes", retained)

    def _pending_auto_start_operations(self) -> list[dict[str, Any]]:
        operations = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_auto_start_operations ORDER BY rowid"
            )
        ]
        operations.sort(
            key=lambda operation: (
                int(operation.get("hlcWallMs", 0)),
                int(operation.get("hlcCounter", 0)),
                str(operation.get("deviceId", "")),
                str(operation.get("id", "")),
            )
        )
        return operations

    def _pending_selected_task_operations(self) -> list[dict[str, Any]]:
        operations = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_selected_task_operations ORDER BY rowid"
            )
        ]
        operations.sort(
            key=lambda operation: (
                int(operation.get("hlcWallMs", 0)),
                int(operation.get("hlcCounter", 0)),
                str(operation.get("deviceId", "")),
                str(operation.get("id", "")),
            )
        )
        return operations

    @staticmethod
    def _pending_object(payload: Any, label: str) -> dict[str, Any]:
        try:
            operation = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Pending {label} contains invalid JSON.") from error
        if not isinstance(operation, dict):
            raise ValueError(f"Pending {label} must be an object.")
        return operation

    @staticmethod
    def _valid_identity(value: Any) -> bool:
        return isinstance(value, str) and bool(value)

    def _validate_pending_command(
        self, command: dict[str, Any], row: sqlite3.Row
    ) -> None:
        sequence = self._bounded_integer(
            command.get("deviceSequence"), "Pending timer command sequence", minimum=1
        )
        try:
            planned_ms = self._duration_ms(
                command.get("plannedDurationMs"),
                maximum=CANONICAL_DURATION_MAX_MS,
            )
        except ValueError as error:
            raise ValueError("Pending timer command duration is invalid.") from error
        observed_ms = command.get("observedElapsedMs")
        task_id = command.get("taskId")
        if (
            not self._valid_identity(command.get("id"))
            or command["id"] != row["id"]
            or sequence != row["device_sequence"]
            or not self._valid_identity(command.get("timerId"))
            or command.get("type") not in COMMAND_TYPES
            or command.get("phase") not in PHASES
            or isinstance(observed_ms, bool)
            or not isinstance(observed_ms, int)
            or not 0 <= observed_ms <= planned_ms
            or command.get("type") == "start"
            and observed_ms != 0
            or not isinstance(task_id, (str, type(None)))
            or isinstance(task_id, str)
            and not task_id
            or task_id is not None
            and (
                command.get("type") != "start"
                or command.get("phase") != "focus"
            )
        ):
            raise ValueError("Pending timer command is invalid.")
        self._operation_clock(command)

    def _validate_pending_task_operation(
        self, operation: dict[str, Any], row: sqlite3.Row
    ) -> None:
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or not self._valid_identity(operation.get("taskId"))
            or operation.get("type") not in {"upsert", "delete"}
        ):
            raise ValueError("Pending task operation is invalid.")
        if operation["type"] == "upsert":
            title = operation.get("title")
            try:
                task = task_from_title(title) if isinstance(title, str) else None
            except ValueError:
                task = None
            if task is None or task["id"] != operation["taskId"]:
                raise ValueError("Pending task operation is invalid.")
        elif "title" in operation:
            raise ValueError("Pending task operation is invalid.")
        self._operation_clock(operation)

    def _validate_pending_duration_operation(
        self, operation: dict[str, Any], row: sqlite3.Row
    ) -> None:
        try:
            self._duration_ms(operation.get("durationMs"))
        except ValueError as error:
            raise ValueError("Pending duration operation is invalid.") from error
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("phase") not in PHASES
            or operation["phase"] != row["phase"]
        ):
            raise ValueError("Pending duration operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _validate_pending_auto_start_operation(
        self, operation: dict[str, Any], row: sqlite3.Row, device_id: str
    ) -> None:
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("deviceId") != device_id
            or not isinstance(operation.get("enabled"), bool)
        ):
            raise ValueError("Pending auto-start operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _validate_pending_selected_task_operation(
        self, operation: dict[str, Any], row: sqlite3.Row, device_id: str
    ) -> None:
        task_id = operation.get("taskId")
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("deviceId") != device_id
            or not isinstance(task_id, (str, type(None)))
            or isinstance(task_id, str)
            and not task_id
        ):
            raise ValueError("Pending selected-task operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _preflight_pending_queues(
        self, *, require_clock_coverage: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        device_id = self.get_meta("deviceId")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("Persisted device identity is invalid.")
        persisted_sequence = self._bounded_integer(
            self.get_meta("deviceSequence", 0), "Persisted device sequence"
        )
        persisted_clock = self._logical_clock(
            self.get_meta("hlc", {"wallMs": 0, "counter": 0}),
            allow_legacy_zero=True,
        )
        self._server_clock_sample(self.get_meta("serverClockSample"))

        commands: list[dict[str, Any]] = []
        sendable_commands: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT id, device_sequence, payload, depends_on_command_id "
            "FROM pending_commands ORDER BY device_sequence"
        ):
            command = self._pending_object(row["payload"], "timer command")
            self._validate_pending_command(command, row)
            commands.append(command)
            if row["depends_on_command_id"] is None:
                sendable_commands.append(command)
        if commands and commands[-1]["deviceSequence"] > persisted_sequence:
            raise ValueError("Pending timer command exceeds persisted device sequence.")

        task_operations = []
        for row in self.connection.execute(
            "SELECT id, payload FROM pending_task_operations ORDER BY rowid"
        ):
            operation = self._pending_object(row["payload"], "task operation")
            self._validate_pending_task_operation(operation, row)
            task_operations.append(operation)

        duration_operations = []
        for row in self.connection.execute(
            "SELECT id, phase, payload FROM pending_duration_operations ORDER BY rowid"
        ):
            operation = self._pending_object(row["payload"], "duration operation")
            self._validate_pending_duration_operation(operation, row)
            duration_operations.append(operation)
        duration_operations.sort(
            key=lambda operation: (
                operation["hlcWallMs"],
                operation["hlcCounter"],
                operation["id"],
            )
        )

        auto_start_operations = []
        for row in self.connection.execute(
            "SELECT id, payload FROM pending_auto_start_operations ORDER BY rowid"
        ):
            operation = self._pending_object(row["payload"], "auto-start operation")
            self._validate_pending_auto_start_operation(operation, row, device_id)
            auto_start_operations.append(operation)
        auto_start_operations.sort(
            key=lambda operation: (
                operation["hlcWallMs"],
                operation["hlcCounter"],
                operation["deviceId"],
                operation["id"],
            )
        )
        selected_task_operations = []
        for row in self.connection.execute(
            "SELECT id, payload FROM pending_selected_task_operations ORDER BY rowid"
        ):
            operation = self._pending_object(row["payload"], "selected-task operation")
            self._validate_pending_selected_task_operation(operation, row, device_id)
            selected_task_operations.append(operation)
        selected_task_operations.sort(
            key=lambda operation: (
                operation["hlcWallMs"],
                operation["hlcCounter"],
                operation["deviceId"],
                operation["id"],
            )
        )
        clocks = [
            (operation["hlcWallMs"], operation["hlcCounter"])
            for operations in (
                commands,
                task_operations,
                duration_operations,
                auto_start_operations,
                selected_task_operations,
            )
            for operation in operations
        ]
        if require_clock_coverage and clocks and max(clocks) > persisted_clock:
            raise ValueError("Pending operation exceeds persisted logical clock.")
        return {
            "commands": commands,
            "sendableCommands": sendable_commands,
            "taskOperations": task_operations,
            "durationOperations": duration_operations,
            "autoStartOperations": auto_start_operations,
            "selectedTaskOperations": selected_task_operations,
        }

    def save_settings(self, settings: dict[str, Any]) -> None:
        candidate = self._normalize_settings(settings)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            current = self._normalize_settings(self.get_meta("settings", {}))
            candidate["durations"] = current["durations"]
            candidate["durationsMs"] = current["durationsMs"]
            candidate["autoStartBreaks"] = current["autoStartBreaks"]
            candidate["selectedTaskId"] = current["selectedTaskId"]
            self._set_meta("settings", candidate)

    def _set_local_setting(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings[key] = value
            self._set_meta("settings", settings)
            self._capture_iroh_after_mutation_locked()

    def set_selected_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings["selectedPhase"] = phase
            self._set_meta("settings", settings)
            version = int(self.get_meta("selectedPhaseVersion", 0)) + 1
            self._set_meta("selectedPhaseVersion", version)
            self._capture_iroh_after_mutation_locked()

    def set_auto_start_breaks(
        self, enabled: bool, now_ms: int | None = None
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("Auto-start preference must be true or false.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_auto_start_operation(
                enabled,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._set_meta("autoStartLegacyDefaultUnknown", False)
            self._capture_iroh_after_mutation_locked()
        return operation

    def set_selected_task_id(
        self, task_id: str | None, now_ms: int | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(task_id, (str, type(None))) or isinstance(task_id, str) and not task_id:
            raise ValueError("Selected task identity must be non-empty or null.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_selected_task_operation(
                task_id,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._capture_iroh_after_mutation_locked()
        return operation

    def queue_command(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
        generate_auto_break: bool = False,
    ) -> dict[str, Any]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            effective_now_ms = (
                now_ms
                if not use_server_clock
                else self.effective_timer_now_ms(timer, physical_ms=now_ms)
            )
            state = self.load()
            settings = self._normalize_settings(state["settings"])
            if command_type == "start":
                selected_task_id = settings.get("selectedTaskId")
                tasks = rebuild_tasks(
                    state["snapshot"].get("tasks", []), state["pendingTasks"]
                )
                if not any(
                    task.get("id") == selected_task_id for task in tasks
                ):
                    selected_task_id = None
            generates_break = bool(
                generate_auto_break
                and command_type == "finish"
                and isinstance(timer, dict)
                and timer.get("phase") == "focus"
                and settings["autoStartBreaks"]
                and (
                    self.replication_mode != "iroh"
                    or timer.get("startedByDeviceId") == self.device_id
                )
            )
            trusted_ms, sequences, clocks = self._reserve_generation(
                effective_now_ms,
                sequence_count=2 if generates_break else 1,
                clock_count=2 if generates_break else 1,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            command_ids = self._reserve_uuid7_ids(
                clocks[0][0], 2 if generates_break else 1
            )
            command = self._queue_command(
                command_type,
                timer,
                selected_phase,
                durations_ms,
                selected_task_id,
                now_ms,
                timer_now_ms=effective_now_ms,
                trusted_ms=trusted_ms,
                sequence=sequences[0],
                clock=clocks[0],
                command_id=command_ids[0],
            )
            if generates_break:
                self._queue_generated_auto_break(
                    command,
                    now_ms,
                    timer_now_ms=effective_now_ms,
                    trusted_ms=trusted_ms,
                    sequence=sequences[1],
                    clock=clocks[1],
                    command_id=command_ids[1],
                )
            state = self.load(projection=True)
            projection_snapshot = state.get("projectionSnapshot", state["snapshot"])
            projected_timer, _history = rebuild_optimistic(
                projection_snapshot.get("canonicalTimer"),
                projection_snapshot.get("history", []),
                state.get("projectionPending", state["pending"]),
            )
            if (
                use_server_clock
                and projected_timer
                and projected_timer.get("status") == "running"
            ):
                self.effective_timer_now_ms(
                    projected_timer,
                    physical_ms=effective_now_ms,
                )
            self._capture_iroh_after_mutation_locked()
        return command

    def queue_restart(
        self,
        timer: dict[str, Any],
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            state = self.load(projection=True)
            projection_snapshot = state.get("projectionSnapshot", state["snapshot"])
            current_timer, _history = rebuild_optimistic(
                projection_snapshot.get("canonicalTimer"),
                projection_snapshot.get("history", []),
                state.get("projectionPending", state["pending"]),
            )
            if (
                not isinstance(current_timer, dict)
                or current_timer.get("status") not in {
                    "completed",
                    "cancelled",
                    "superseded",
                }
                or self._timer_fingerprint(current_timer)
                != self._timer_fingerprint(timer)
            ):
                raise ValueError("Timer changed before restart could be saved.")
            settings = self._normalize_settings(state["settings"])
            selected_phase = settings["selectedPhase"]
            durations_ms = settings["durationsMs"]
            selected_task_id = settings.get("selectedTaskId")
            tasks = rebuild_tasks(
                state["snapshot"].get("tasks", []), state["pendingTasks"]
            )
            if not any(task.get("id") == selected_task_id for task in tasks):
                selected_task_id = None
            effective_now_ms = (
                now_ms
                if not use_server_clock
                else self.effective_timer_now_ms(current_timer, physical_ms=now_ms)
            )
            trusted_ms, sequences, clocks = self._reserve_generation(
                now_ms,
                sequence_count=2,
                clock_count=2,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            command_ids = self._reserve_uuid7_ids(clocks[0][0], 2)
            cleared = self._queue_command(
                "clear",
                current_timer,
                selected_phase,
                durations_ms,
                selected_task_id,
                now_ms,
                timer_now_ms=effective_now_ms,
                trusted_ms=trusted_ms,
                sequence=sequences[0],
                clock=clocks[0],
                command_id=command_ids[0],
            )
            started = self._queue_command(
                "start",
                None,
                selected_phase,
                durations_ms,
                selected_task_id,
                now_ms,
                timer_now_ms=effective_now_ms,
                trusted_ms=trusted_ms,
                sequence=sequences[1],
                clock=clocks[1],
                command_id=command_ids[1],
            )
            self._capture_iroh_after_mutation_locked()
        return [cleared, started]

    def queue_cancel_and_clear(
        self,
        timer: dict[str, Any],
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            state = self.load(projection=True)
            projection_snapshot = state.get("projectionSnapshot", state["snapshot"])
            current_timer, _history = rebuild_optimistic(
                projection_snapshot.get("canonicalTimer"),
                projection_snapshot.get("history", []),
                state.get("projectionPending", state["pending"]),
            )
            if (
                not isinstance(current_timer, dict)
                or current_timer.get("status") not in ACTIVE_STATUSES
                or self._timer_fingerprint(current_timer)
                != self._timer_fingerprint(timer)
            ):
                raise ValueError("Timer changed before cancel could be saved.")
            settings = self._normalize_settings(state["settings"])
            selected_phase = settings["selectedPhase"]
            durations_ms = settings["durationsMs"]
            selected_task_id = settings.get("selectedTaskId")
            effective_now_ms = (
                now_ms
                if not use_server_clock
                else self.effective_timer_now_ms(current_timer, physical_ms=now_ms)
            )
            trusted_ms, sequences, clocks = self._reserve_generation(
                effective_now_ms,
                sequence_count=2,
                clock_count=2,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            command_ids = self._reserve_uuid7_ids(clocks[0][0], 2)
            cancelled = self._queue_command(
                "cancel",
                current_timer,
                selected_phase,
                durations_ms,
                selected_task_id,
                now_ms,
                timer_now_ms=effective_now_ms,
                trusted_ms=trusted_ms,
                sequence=sequences[0],
                clock=clocks[0],
                command_id=command_ids[0],
            )
            cleared = self._queue_command(
                "clear",
                current_timer,
                selected_phase,
                durations_ms,
                selected_task_id,
                now_ms,
                timer_now_ms=effective_now_ms,
                trusted_ms=trusted_ms,
                sequence=sequences[1],
                clock=clocks[1],
                command_id=command_ids[1],
            )
            self._capture_iroh_after_mutation_locked()
        return [cancelled, cleared]

    def _queue_command(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None,
        now_ms: int,
        depends_on_command_id: str | None = None,
        *,
        timer_now_ms: int | None = None,
        trusted_ms: int | None = None,
        sequence: int | None = None,
        clock: tuple[int, int] | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        if command_type not in COMMAND_TYPES:
            raise ValueError("Unsupported timer command.")
        if sequence is None or clock is None:
            trusted_ms, sequences, clocks = self._reserve_generation(
                now_ms, sequence_count=1, clock_count=1
            )
            sequence = sequences[0]
            clock = clocks[0]
        if trusted_ms is None:
            trusted_ms = self._trusted_now_ms(now_ms, use_monotonic=False)
        timer_now_ms = now_ms if timer_now_ms is None else timer_now_ms
        wall_ms, counter = clock
        if command_id is None:
            command_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        starting = command_type == "start"

        if starting:
            phase = selected_phase if selected_phase in PHASES else "focus"
            timer_id = str(uuid.uuid4())
            planned_ms = self._duration_ms(durations_ms[phase])
            observed_ms = 0
        else:
            if not timer or not timer.get("id"):
                raise ValueError("No timer is available for this action.")
            phase = str(timer["phase"])
            timer_id = str(timer["id"])
            planned_ms = int(timer["plannedDurationMs"])
            observed_ms = round(elapsed_ms(timer, timer_now_ms))
            if depends_on_command_id is None:
                for row in self.connection.execute(
                    "SELECT depends_on_command_id, payload FROM pending_commands "
                    "WHERE depends_on_command_id IS NOT NULL"
                ):
                    pending_command = json.loads(row["payload"])
                    if pending_command.get("timerId") == timer_id:
                        depends_on_command_id = str(row["depends_on_command_id"])
                        break

        command = {
            "id": command_id,
            "deviceSequence": sequence,
            "timerId": timer_id,
            "type": command_type,
            "phase": phase,
            "plannedDurationMs": planned_ms,
            "occurredAt": utc_timestamp(trusted_ms),
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
            "observedElapsedMs": observed_ms,
        }
        if starting and phase == "focus" and selected_task_id:
            command["taskId"] = selected_task_id
        self.connection.execute(
            "INSERT INTO pending_commands("
            "id, device_sequence, payload, depends_on_command_id) VALUES (?, ?, ?, ?)",
            (
                command["id"],
                sequence,
                json.dumps(command, separators=(",", ":")),
                depends_on_command_id,
            ),
        )
        self._record_command_physical_time(command["id"], timer_now_ms)
        if starting and self.replication_mode != "iroh":
            self._set_meta(
                "centralizedTimerOwnership",
                {
                    "timerId": timer_id,
                    "deviceId": self.device_id,
                    "startCommandId": command["id"],
                },
            )
        if command_type == "finish":
            settings = self._normalize_settings(self.get_meta("settings", {}))
            snapshot = self.get_meta("snapshot", {})
            _, history = rebuild_optimistic(
                snapshot.get("canonicalTimer"),
                snapshot.get("history", []),
                self._pending_commands(),
            )
            advanced_phase = (
                next_break_phase(history, command["occurredAt"])
                if phase == "focus"
                else "focus"
            )
            settings["selectedPhase"] = advanced_phase
            self._set_meta("settings", settings)
            self.connection.execute(
                "INSERT INTO pending_phase_advances("
                "finish_command_id, timer_id, source_phase, advanced_phase, "
                "selected_phase_version) VALUES (?, ?, ?, ?, ?)",
                (
                    command["id"],
                    timer_id,
                    phase,
                    advanced_phase,
                    int(self.get_meta("selectedPhaseVersion", 0)),
                ),
            )
            if (
                phase == "focus"
                and settings["autoStartBreaks"]
                and (
                    self.replication_mode != "iroh"
                    or timer is not None
                    and timer.get("startedByDeviceId") == self.device_id
                )
            ):
                self.connection.execute(
                    "INSERT OR IGNORE INTO pending_auto_breaks("
                    "finish_command_id, timer_id, finish_device_sequence) "
                    "VALUES (?, ?, ?)",
                    (command["id"], timer_id, sequence),
                )
        self._set_meta("deviceSequence", sequence)
        self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return command

    def _queue_generated_auto_break(
        self,
        finish: dict[str, Any],
        now_ms: int,
        *,
        timer_now_ms: int,
        trusted_ms: int,
        sequence: int,
        clock: tuple[int, int],
        command_id: str,
    ) -> dict[str, Any]:
        snapshot = self.get_meta("snapshot", {})
        _timer, history = rebuild_optimistic(
            snapshot.get("canonicalTimer"),
            snapshot.get("history", []),
            self._pending_commands(),
        )
        completion = next(
            (
                item
                for item in history
                if item.get("phase") == "focus"
                and item.get("status") == "completed"
                and item.get("timerId") == finish["timerId"]
                and item.get("commandId") == finish["id"]
            ),
            None,
        )
        if completion is None:
            raise ValueError("Automatic break generation requires an accepted focus finish.")
        settings = self._normalize_settings(self.get_meta("settings", {}))
        phase = next_break_phase(
            history, completion.get("completedAt") or completion.get("endedAt")
        )
        settings["selectedPhase"] = phase
        self._set_meta("settings", settings)
        self.connection.execute(
            "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
            (finish["id"],),
        )
        command = self._queue_command(
            "start",
            None,
            phase,
            settings["durationsMs"],
            None,
            now_ms,
            finish["id"],
            timer_now_ms=timer_now_ms,
            trusted_ms=trusted_ms,
            sequence=sequence,
            clock=clock,
            command_id=command_id,
        )
        self.connection.execute(
            "INSERT INTO pending_auto_break_starts("
            "source_finish_command_id, source_timer_id, start_command_id, "
            "selected_phase_version) VALUES (?, ?, ?, ?)",
            (
                finish["id"],
                finish["timerId"],
                command["id"],
                int(self.get_meta("selectedPhaseVersion", 0)),
            ),
        )
        return command

    def queue_task_operation(
        self,
        operation_type: str,
        task: dict[str, str],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if operation_type not in {"upsert", "delete"}:
            raise ValueError("Unsupported task operation.")
        normalized = task_from_title(task.get("title", ""))
        if normalized["id"] != task.get("id"):
            raise ValueError("Task identity does not match its name.")

        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            trusted_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
            operation: dict[str, Any] = {
                "id": operation_id,
                "taskId": normalized["id"],
                "type": operation_type,
                "occurredAt": utc_timestamp(trusted_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": counter,
            }
            if operation_type == "upsert":
                operation["title"] = normalized["title"]

            self.connection.execute(
                "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
                (operation["id"], json.dumps(operation, separators=(",", ":"))),
            )
            snapshot = self.get_meta("snapshot", {})
            known = {
                item["id"]: item
                for item in snapshot.get("knownTasks", [])
                if item.get("id") and item.get("title")
            }
            known[normalized["id"]] = normalized
            snapshot["knownTasks"] = sorted(
                known.values(),
                key=lambda item: (item["title"].encode(), item["id"].encode()),
            )
            self._set_meta("snapshot", snapshot)
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
            self._capture_iroh_after_mutation_locked()
        return operation

    def queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_duration_operation(
                phase,
                duration_ms,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._capture_iroh_after_mutation_locked()
        return operation

    def _queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        duration_ms = self._duration_ms(duration_ms)
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "phase": phase,
            "durationMs": duration_ms,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        self.connection.execute(
            "INSERT INTO pending_duration_operations(id, phase, payload) "
            "VALUES (?, ?, ?) ON CONFLICT(phase) DO UPDATE SET "
            "id = excluded.id, payload = excluded.payload",
            (
                operation["id"],
                phase,
                json.dumps(operation, separators=(",", ":")),
            ),
        )
        settings["durationsMs"][phase] = duration_ms
        settings["durations"][phase] = self._display_minutes(duration_ms)
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    def _queue_auto_start_operation(
        self,
        enabled: bool,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "deviceId": self.device_id,
            "enabled": enabled,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        self.connection.execute(
            "INSERT INTO pending_auto_start_operations(id, payload) VALUES (?, ?)",
            (operation["id"], json.dumps(operation, separators=(",", ":"))),
        )
        settings["autoStartBreaks"] = enabled
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    def _queue_selected_task_operation(
        self,
        task_id: str | None,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "deviceId": self.device_id,
            "taskId": task_id,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        self.connection.execute(
            "INSERT INTO pending_selected_task_operations(id, payload) VALUES (?, ?)",
            (operation["id"], json.dumps(operation, separators=(",", ":"))),
        )
        settings["selectedTaskId"] = task_id
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    @staticmethod
    def _wire_preference_operations(
        operations: Any, error_message: str
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or any(
            not isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(error_message)
        outbound = []
        for operation in operations:
            item = dict(operation)
            item.pop("deviceId", None)
            outbound.append(item)
        return outbound

    def _replace_meta_inside_or_outside_transaction(
        self, key: str, value: Any
    ) -> None:
        if self.connection.in_transaction:
            self._set_meta(key, value)
        else:
            self.set_meta(key, value)

    def sync_payload(self) -> dict[str, Any]:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            claimed = self.pending_sync()
            if claimed is not None:
                return claimed
            pending = self._preflight_pending_queues()
            snapshot = self.get_meta("snapshot", {})
            revision = self._bounded_integer(
                snapshot.get("revision", 0), "Persisted revision"
            )
            payload = {
                "deviceId": self.device_id,
                "lastRevision": revision,
                "commands": pending["sendableCommands"][:256],
                "taskOperations": pending["taskOperations"][:256],
                "durationOperations": pending["durationOperations"][:256],
                "autoStartOperations": self._wire_preference_operations(
                    pending["autoStartOperations"][:256],
                    "Pending auto-start operation is corrupted.",
                ),
                "selectedTaskOperations": self._wire_preference_operations(
                    pending["selectedTaskOperations"][:256],
                    "Pending selected-task operation is corrupted.",
                ),
            }
            self._set_meta("pendingSync", payload)
        return payload

    def pending_sync(self) -> dict[str, Any] | None:
        pending = self.get_meta("pendingSync")
        if pending is None:
            return None
        current_keys = {
            "deviceId",
            "lastRevision",
            "commands",
            "taskOperations",
            "durationOperations",
            "autoStartOperations",
            "selectedTaskOperations",
        }
        legacy_keys = current_keys - {"selectedTaskOperations"}
        original = deepcopy(pending)
        if isinstance(pending, dict) and set(pending) == legacy_keys:
            pending = {**pending, "selectedTaskOperations": []}
        if (
            not isinstance(pending, dict)
            or set(pending) != current_keys
            or pending.get("deviceId") != self.device_id
            or any(
                not isinstance(pending.get(key), list)
                for key in (
                    "commands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                    "selectedTaskOperations",
                )
            )
        ):
            raise ValueError("Pending normal sync claim is corrupted.")
        self._bounded_integer(
            pending.get("lastRevision"), "Pending normal sync revision"
        )
        pending = {
            **pending,
            "autoStartOperations": self._wire_preference_operations(
                pending["autoStartOperations"],
                "Pending normal sync claim is corrupted.",
            ),
            "selectedTaskOperations": self._wire_preference_operations(
                pending["selectedTaskOperations"],
                "Pending normal sync claim is corrupted.",
            ),
        }
        if pending != original:
            self._replace_meta_inside_or_outside_transaction("pendingSync", pending)
        return pending

    def has_sendable_sync_operations(self) -> bool:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            pending = self._preflight_pending_queues()
            return any(
                pending[key]
                for key in (
                    "sendableCommands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                    "selectedTaskOperations",
                )
            )

    def pending_resolution(self, user_id: str | None = None) -> dict[str, Any] | None:
        try:
            pending = self.get_meta("pendingResolution")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Pending account history is corrupted.") from error
        if pending is None:
            return None
        if not isinstance(pending, dict):
            raise ValueError("Pending account history is corrupted.")
        owner = pending.get("owner")
        request = pending.get("request")
        queue_ids = pending.get("queueIds")
        if (
            not isinstance(owner, dict)
            or not isinstance(request, dict)
            or not isinstance(queue_ids, dict)
            or set(queue_ids)
            not in (
                {"commands", "taskOperations", "durationOperations"},
                {
                    "commands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                },
                {
                    "commands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                    "selectedTaskOperations",
                },
            )
            or any(
                not isinstance(ids, list)
                or any(not isinstance(item_id, str) for item_id in ids)
                or len(ids) != len(set(ids))
                for ids in queue_ids.values()
            )
            or not isinstance(owner.get("id"), str)
            or not owner["id"]
            or not isinstance(request.get("requestId"), str)
            or not request["requestId"]
            or request.get("deviceId") != self.device_id
            or request.get("strategy")
            not in {"keep_remote", "replace_remote", "merge"}
        ):
            raise ValueError("Pending account history is corrupted.")
        normalized_request = dict(request)
        for key in ("autoStartOperations", "selectedTaskOperations"):
            if key in normalized_request:
                normalized_request[key] = self._wire_preference_operations(
                    normalized_request[key],
                    "Pending account history is corrupted.",
                )
        if normalized_request != request:
            pending = {**pending, "request": normalized_request}
            self._replace_meta_inside_or_outside_transaction(
                "pendingResolution", pending
            )
        if user_id is not None and owner.get("id") != user_id:
            return None
        return pending

    def clear_pending_resolution(self) -> None:
        self.set_meta("pendingResolution", None)

    def _ensure_no_pending_resolution(self) -> None:
        if self.pending_resolution() is not None:
            raise ValueError("Resolve pending account history before making changes.")

    def discard_pending_resolution(self, user_id: str, request_id: str) -> bool:
        with self._immediate_transaction():
            pending = self.pending_resolution(user_id)
            if (
                pending is None
                or pending["request"].get("requestId") != request_id
            ):
                return False
            self._set_meta("pendingResolution", None)
        return True

    def bootstrap_resolution_plan(
        self,
        response: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> dict[str, Any]:
        canonical = self._validated_sync_response(
            response,
            {
                "commands": [],
                "taskOperations": [],
                "durationOperations": [],
                "autoStartOperations": [],
                "selectedTaskOperations": [],
            },
        )
        with self._immediate_transaction():
            self._preflight_pending_queues()
            sample, anchor = self._clock_sample_for_response(
                canonical["serverTimeMs"],
                request_physical_ms,
                received_physical_ms,
                request_monotonic_ms,
                received_monotonic_ms,
            )
            state = self.load()
            if sample is not None:
                self._set_meta("serverClockSample", sample)
        if anchor is not None:
            self._set_trusted_time_anchor(anchor)
        local_timer, local_history = rebuild_optimistic(
            state["snapshot"].get("canonicalTimer"),
            state["snapshot"].get("history", []),
            state["pending"],
        )
        local_history_exists = any(
            item.get("status") == "completed" for item in local_history
        )
        remote_history_exists = any(
            item.get("status") == "completed" for item in canonical["history"]
        )
        local_state_exists = bool(
            state["pending"]
            or state["pendingTasks"]
            or state["pendingDurations"]
            or state["pendingAutoStarts"]
            or state["pendingSelectedTasks"]
            or state["snapshot"].get("canonicalTimer")
            or state["snapshot"].get("history")
            or state["snapshot"].get("tasks")
            or state["snapshot"].get("selectedTaskId") is not None
            or state["settings"].get("selectedTaskId") is not None
            or state["settings"].get("autoStartBreaks")
            or any(
                state["settings"].get("durationsMs", {}).get(phase)
                != definition["default_minutes"] * 60_000
                for phase, definition in PHASES.items()
            )
        )
        remote_state_exists = bool(
            canonical["canonicalTimer"]
            or canonical["history"]
            or canonical["tasks"]
            or canonical["selectedTaskId"] is not None
            or canonical["autoStartBreaks"]
            or any(
                canonical["durationsMs"].get(phase)
                != definition["default_minutes"] * 60_000
                for phase, definition in PHASES.items()
            )
        )
        local_state_exists = local_state_exists or local_timer is not None
        if (
            local_history_exists and remote_state_exists
        ) or (
            remote_history_exists and local_state_exists
        ):
            strategy = None
        elif local_history_exists:
            strategy = "replace_remote"
        elif remote_history_exists:
            strategy = "keep_remote"
        elif local_state_exists:
            strategy = "merge"
        else:
            strategy = "keep_remote"
        return {
            "expectedRevision": canonical["revision"],
            "localHistory": local_history_exists,
            "remoteHistory": remote_history_exists,
            "strategy": strategy,
        }

    def prepare_resolution(
        self,
        user: dict[str, Any],
        expected_revision: int,
        strategy: str,
    ) -> dict[str, Any]:
        if strategy not in {"keep_remote", "replace_remote", "merge"}:
            raise ValueError("Unsupported history resolution strategy.")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or not 0 <= expected_revision <= MAX_SAFE_INTEGER
        ):
            raise ValueError("Bootstrap returned an invalid revision.")
        user_id = user.get("id") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("Signed-in user has no stable identity.")

        with self._immediate_transaction():
            pending = self.pending_resolution()
            if pending is not None:
                if pending["owner"].get("id") != user_id:
                    raise ValueError(
                        "Pending account history belongs to another account."
                    )
                return pending["request"]
            if self.pending_sync() is not None:
                raise ValueError(
                    "Finish pending normal sync before resolving account history."
                )
            validated_pending = self._preflight_pending_queues()
            state = self.load(projection=True)
            operations = state if strategy != "keep_remote" else None
            sendable_commands = validated_pending["sendableCommands"]
            outbound = {
                "commands": sendable_commands if operations else [],
                "taskOperations": (
                    validated_pending["taskOperations"] if operations else []
                ),
                "durationOperations": (
                    validated_pending["durationOperations"] if operations else []
                ),
                "autoStartOperations": (
                    self._wire_preference_operations(
                        validated_pending["autoStartOperations"],
                        "Pending auto-start operation is corrupted.",
                    )
                    if operations
                    else []
                ),
                "selectedTaskOperations": (
                    self._wire_preference_operations(
                        validated_pending["selectedTaskOperations"],
                        "Pending selected-task operation is corrupted.",
                    )
                    if operations
                    else []
                ),
            }
            if (
                strategy == "replace_remote"
                and self.get_meta("autoStartLegacyDefaultUnknown", False)
                and not state["pendingAutoStarts"]
            ):
                outbound.pop("autoStartOperations")
            labels = {
                "commands": "timer commands",
                "taskOperations": "task operations",
                "durationOperations": "duration operations",
                "autoStartOperations": "auto-start operations",
                "selectedTaskOperations": "selected-task operations",
            }
            for key, items in outbound.items():
                if len(items) > RESOLUTION_OPERATION_MAX:
                    raise ValueError(
                        f"History resolution supports at most "
                        f"{RESOLUTION_OPERATION_MAX} {labels[key]}."
                    )
            request = {
                "requestId": str(uuid.uuid4()),
                "deviceId": self.device_id,
                "expectedRevision": expected_revision,
                "strategy": strategy,
                **outbound,
            }
            queue_ids = {
                "commands": [
                    item["id"]
                    for item in (
                        validated_pending["commands"]
                        if strategy == "keep_remote"
                        else sendable_commands
                    )
                ],
                "taskOperations": [
                    item["id"] for item in validated_pending["taskOperations"]
                ],
                "durationOperations": [
                    item["id"] for item in validated_pending["durationOperations"]
                ],
                "autoStartOperations": [
                    item["id"] for item in validated_pending["autoStartOperations"]
                ],
                "selectedTaskOperations": [
                    item["id"] for item in validated_pending["selectedTaskOperations"]
                ],
            }
            self._set_meta(
                "pendingResolution",
                {"owner": user, "request": request, "queueIds": queue_ids},
            )
        return request

    @staticmethod
    def _validate_acknowledgements(
        request_items: Any,
        response_items: Any,
        acknowledgement_id_key: str,
        label: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(request_items, list) or not isinstance(response_items, list):
            raise ValueError(f"Sync returned invalid {label} acknowledgements.")
        sent_ids = [
            item.get("id") if isinstance(item, dict) else None
            for item in request_items
        ]
        acknowledged_ids: list[str] = []
        normalized_response_items: list[dict[str, Any]] = []
        for acknowledgement in response_items:
            if not isinstance(acknowledgement, dict):
                raise ValueError(f"Sync returned invalid {label} acknowledgements.")
            normalized_acknowledgement = {
                **acknowledgement,
                "reason": acknowledgement.get("reason", ""),
            }
            if (
                not isinstance(normalized_acknowledgement.get(acknowledgement_id_key), str)
                or normalized_acknowledgement.get("outcome") not in ACKNOWLEDGEMENT_OUTCOMES
                or not isinstance(normalized_acknowledgement["reason"], str)
            ):
                raise ValueError(f"Sync returned invalid {label} acknowledgements.")
            acknowledged_ids.append(normalized_acknowledgement[acknowledgement_id_key])
            normalized_response_items.append(normalized_acknowledgement)
        if (
            any(not isinstance(item_id, str) for item_id in sent_ids)
            or len(sent_ids) != len(set(sent_ids))
            or len(acknowledged_ids) != len(set(acknowledged_ids))
            or set(acknowledged_ids) != set(sent_ids)
        ):
            raise ValueError(
                f"Sync returned an invalid {label} acknowledgement set."
            )
        return normalized_response_items

    def _validated_sync_response(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ValueError("Server returned an invalid sync response.")
        required = {
            "acknowledgements",
            "taskAcknowledgements",
            "durationAcknowledgements",
            "autoStartAcknowledgements",
            "selectedTaskAcknowledgements",
            "revision",
            "canonicalTimer",
            "history",
            "tasks",
            "durationsMs",
            "autoStartBreaks",
            "selectedTaskId",
            "serverTime",
            "serverHlcWallMs",
            "serverHlcCounter",
        }
        missing = required - set(response)
        if missing:
            raise ValueError(
                "Server response omitted canonical fields: "
                + ", ".join(sorted(missing))
                + "."
            )
        acknowledgements = self._validate_acknowledgements(
            request.get("commands"), response["acknowledgements"],
            "commandId",
            "command",
        )
        task_acknowledgements = self._validate_acknowledgements(
            request.get("taskOperations"), response["taskAcknowledgements"],
            "operationId",
            "task",
        )
        duration_acknowledgements = self._validate_acknowledgements(
            request.get("durationOperations"), response["durationAcknowledgements"],
            "operationId",
            "duration",
        )
        auto_start_acknowledgements = self._validate_acknowledgements(
            request.get("autoStartOperations", []),
            response["autoStartAcknowledgements"],
            "operationId",
            "auto-start",
        )
        selected_task_operations = request.get("selectedTaskOperations", [])
        selected_task_acknowledgements = self._validate_acknowledgements(
            selected_task_operations,
            response["selectedTaskAcknowledgements"],
            "operationId",
            "selected-task",
        )
        canonical_durations = self._canonical_durations(response["durationsMs"])
        auto_start_breaks = response["autoStartBreaks"]
        if not isinstance(auto_start_breaks, bool):
            raise ValueError("Server returned an invalid auto-start preference.")
        revision = response["revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= MAX_SAFE_INTEGER
        ):
            raise ValueError("Server returned an invalid revision.")
        history = response["history"]
        if not isinstance(history, list):
            raise ValueError("Server returned invalid timer history.")
        history_ids: list[str] = []
        for item in history:
            if not self._valid_history_item(item):
                raise ValueError("Server returned invalid timer history.")
            history_ids.append(item["id"])
        if len(history_ids) != len(set(history_ids)):
            raise ValueError("Server returned duplicate timer history.")
        tasks = response["tasks"]
        if not isinstance(tasks, list):
            raise ValueError("Server returned invalid tasks.")
        task_ids: list[str] = []
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("Server returned invalid tasks.")
            task_id = task.get("id")
            title = task.get("title")
            try:
                normalized = task_from_title(title) if isinstance(title, str) else None
            except ValueError:
                normalized = None
            if normalized is None or normalized["id"] != task_id:
                raise ValueError("Server returned invalid tasks.")
            task_ids.append(task_id)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Server returned duplicate tasks.")
        selected_task_id = response["selectedTaskId"]
        if (
            selected_task_id is not None
            and (
                not isinstance(selected_task_id, str)
                or not selected_task_id
                or selected_task_id not in task_ids
            )
        ):
            raise ValueError("Server returned an invalid selected-task preference.")
        canonical_timer = response["canonicalTimer"]
        if canonical_timer is not None and not self._valid_canonical_timer(
            canonical_timer
        ):
            raise ValueError("Server returned an invalid canonical timer.")
        server_hlc_wall_ms = response["serverHlcWallMs"]
        server_hlc_counter = response["serverHlcCounter"]
        server_time = response["serverTime"]
        try:
            server_clock = self._logical_clock(
                {"wallMs": server_hlc_wall_ms, "counter": server_hlc_counter}
            )
            server_time_ms = (
                parse_timestamp_ms(server_time)
                if isinstance(server_time, str)
                else None
            )
            if server_time_ms is None:
                raise ValueError
            self._physical_time_ms(server_time_ms)
            if (
                server_clock[0] < server_time_ms
                or server_clock[0] - server_time_ms > MAX_CLOCK_SKEW_MS
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Server returned an invalid logical clock.")
        return {
            "acknowledgements": acknowledgements,
            "taskAcknowledgements": task_acknowledgements,
            "durationAcknowledgements": duration_acknowledgements,
            "autoStartAcknowledgements": auto_start_acknowledgements,
            "selectedTaskAcknowledgements": selected_task_acknowledgements,
            "revision": revision,
            "canonicalTimer": canonical_timer,
            "history": history,
            "tasks": tasks,
            "durationsMs": canonical_durations,
            "autoStartBreaks": auto_start_breaks,
            "selectedTaskId": selected_task_id,
            "serverTime": server_time,
            "serverTimeMs": server_time_ms,
            "serverHlcWallMs": server_hlc_wall_ms,
            "serverHlcCounter": server_hlc_counter,
        }

    @classmethod
    def _valid_canonical_timer(cls, timer: Any) -> bool:
        if not isinstance(timer, dict):
            return False
        required = {
            "id",
            "phase",
            "status",
            "plannedDurationMs",
            "elapsedAtAnchorMs",
            "anchorAt",
        }
        if required - set(timer):
            return False
        try:
            planned_ms = cls._duration_ms(
                timer["plannedDurationMs"], maximum=CANONICAL_DURATION_MAX_MS
            )
        except ValueError:
            return False
        elapsed = timer["elapsedAtAnchorMs"]
        if (
            not isinstance(timer["id"], str)
            or not timer["id"]
            or timer["phase"] not in PHASES
            or timer["status"]
            not in {"running", "paused", "completed", "cancelled", "superseded"}
            or isinstance(elapsed, bool)
            or not isinstance(elapsed, int)
            or not 0 <= elapsed <= planned_ms
            or not isinstance(timer["anchorAt"], str)
            or parse_timestamp_ms(timer["anchorAt"]) is None
            or not isinstance(timer.get("taskId"), (str, type(None)))
            or isinstance(timer.get("taskId"), str)
            and not timer["taskId"]
        ):
            return False
        intent = timer.get("lastIntent")
        return intent is None or (
            isinstance(intent, dict)
            and intent.get("type")
            in {"start", "pause", "resume", "finish", "cancel", "clear"}
            and isinstance(intent.get("commandId"), str)
            and bool(intent["commandId"])
            and isinstance(intent.get("occurredAt"), str)
            and parse_timestamp_ms(intent["occurredAt"]) is not None
        )

    @classmethod
    def _valid_history_item(cls, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        required = {"id", "timerId", "phase", "status", "plannedDurationMs"}
        if required - set(item):
            return False
        try:
            cls._duration_ms(
                item["plannedDurationMs"], maximum=CANONICAL_DURATION_MAX_MS
            )
        except ValueError:
            return False
        for timestamp_key in ("completedAt", "endedAt"):
            timestamp = item.get(timestamp_key)
            if timestamp is not None and (
                not isinstance(timestamp, str)
                or parse_timestamp_ms(timestamp) is None
            ):
                return False
        return (
            isinstance(item["id"], str)
            and bool(item["id"])
            and isinstance(item["timerId"], str)
            and bool(item["timerId"])
            and item["phase"] in PHASES
            and item["status"] in {"completed", "cancelled", "superseded"}
            and isinstance(item.get("commandId"), (str, type(None)))
            and (item.get("commandId") is None or bool(item["commandId"]))
            and isinstance(item.get("taskId"), (str, type(None)))
            and (item.get("taskId") is None or bool(item["taskId"]))
            and isinstance(item.get("pending", False), bool)
        )

    def _drop_auto_break_start(self, source_finish_command_id: str) -> None:
        dependency = self.connection.execute(
            "SELECT start_command_id, selected_phase_version "
            "FROM pending_auto_break_starts WHERE source_finish_command_id = ?",
            (source_finish_command_id,),
        ).fetchone()
        provisional_phase = None
        if dependency is not None:
            start_row = self.connection.execute(
                "SELECT payload FROM pending_commands WHERE id = ?",
                (dependency["start_command_id"],),
            ).fetchone()
            if start_row is not None:
                try:
                    command = json.loads(start_row["payload"])
                    if command.get("type") == "start":
                        provisional_phase = command.get("phase")
                except (TypeError, json.JSONDecodeError):
                    pass
        self.connection.execute(
            "DELETE FROM pending_commands WHERE depends_on_command_id = ?",
            (source_finish_command_id,),
        )
        self.connection.execute(
            "DELETE FROM pending_auto_break_starts "
            "WHERE source_finish_command_id = ?",
            (source_finish_command_id,),
        )
        self.connection.execute(
            "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
            (source_finish_command_id,),
        )
        if provisional_phase in PHASES and dependency is not None:
            settings = self._normalize_settings(self.get_meta("settings", {}))
            if (
                settings["selectedPhase"] == provisional_phase
                and int(self.get_meta("selectedPhaseVersion", 0))
                == int(dependency["selected_phase_version"])
            ):
                settings["selectedPhase"] = "focus"
                self._set_meta("settings", settings)

    def _transform_auto_break_chain(
        self,
        source_finish_command_id: str,
        start_command_id: str,
        phase: str,
        duration_ms: int,
    ) -> str | None:
        rows = self.connection.execute(
            "SELECT id, device_sequence, payload FROM pending_commands "
            "WHERE depends_on_command_id = ? ORDER BY device_sequence",
            (source_finish_command_id,),
        ).fetchall()
        provisional_phase: str | None = None
        generated_timer_id: str | None = None
        allowed_followups = {"pause", "resume", "finish", "cancel", "clear"}
        parsed_commands: list[dict[str, Any] | None] = []
        for row in rows:
            try:
                parsed = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                parsed = None
            parsed_commands.append(parsed if isinstance(parsed, dict) else None)
        start_command = next(
            (
                command
                for row, command in zip(rows, parsed_commands, strict=True)
                if row["id"] == start_command_id
            ),
            None,
        )
        preserves_completed_break = (
            start_command is not None
            and start_command.get("type") == "start"
            and any(
                command is not None
                and command.get("type") == "finish"
                and command.get("timerId") == start_command.get("timerId")
                for command in parsed_commands
            )
        )
        corrected_phase = (
            str(start_command["phase"])
            if preserves_completed_break
            else phase
        )
        corrected_duration_ms = (
            int(start_command["plannedDurationMs"])
            if preserves_completed_break
            else duration_ms
        )
        for row, command in zip(rows, parsed_commands, strict=True):
            is_generated_start = row["id"] == start_command_id
            valid = isinstance(command, dict)
            if is_generated_start:
                valid = valid and command.get("type") == "start"
                if valid:
                    provisional_phase = str(command["phase"])
                    generated_timer_id = str(command["timerId"])
            else:
                valid = (
                    valid
                    and generated_timer_id is not None
                    and command.get("type") in allowed_followups
                    and command.get("timerId") == generated_timer_id
                )
            if not valid:
                if is_generated_start:
                    return None
                self.connection.execute(
                    "DELETE FROM pending_commands "
                    "WHERE depends_on_command_id = ? AND device_sequence >= ?",
                    (source_finish_command_id, int(row["device_sequence"])),
                )
                break
            command["phase"] = corrected_phase
            command["plannedDurationMs"] = corrected_duration_ms
            if "observedElapsedMs" in command:
                command["observedElapsedMs"] = min(
                    corrected_duration_ms,
                    max(0, int(command.get("observedElapsedMs", 0))),
                )
            self.connection.execute(
                "UPDATE pending_commands SET payload = ? WHERE id = ?",
                (json.dumps(command, separators=(",", ":")), row["id"]),
            )
        return provisional_phase

    def _resolve_auto_break_dependencies(self, canonical: dict[str, Any]) -> None:
        acknowledgements = {
            acknowledgement["commandId"]: acknowledgement
            for acknowledgement in canonical["acknowledgements"]
        }
        if not acknowledgements:
            return
        dependencies = {
            str(row["source_finish_command_id"]): row
            for row in self.connection.execute(
                "SELECT source_finish_command_id, source_timer_id, start_command_id, "
                "selected_phase_version "
                "FROM pending_auto_break_starts"
            )
        }
        triggers = {
            str(row["finish_command_id"]): row
            for row in self.connection.execute(
                "SELECT finish_command_id, timer_id FROM pending_auto_breaks"
            )
        }
        canonical_timer = canonical["canonicalTimer"]
        for source_id, acknowledgement in acknowledgements.items():
            dependency = dependencies.get(source_id)
            trigger = triggers.get(source_id)
            if dependency is None and trigger is None:
                continue
            source_timer_id = str(
                dependency["source_timer_id"]
                if dependency is not None
                else trigger["timer_id"]
            )
            completion = next(
                (
                    item
                    for item in canonical["history"]
                    if item.get("phase") == "focus"
                    and item.get("status") == "completed"
                    and item.get("timerId") == source_timer_id
                    and item.get("commandId") == source_id
                ),
                None,
            )
            timer_is_source = canonical_timer is not None and (
                canonical_timer.get("id") == source_timer_id
                and canonical_timer.get("phase") == "focus"
                and canonical_timer.get("status") == "completed"
            )
            if (
                acknowledgement["outcome"] not in {"applied", "ignored"}
                or completion is None
                or not timer_is_source
            ):
                self._drop_auto_break_start(source_id)
                continue
            if dependency is None:
                continue

            start_row = self.connection.execute(
                "SELECT device_sequence, payload FROM pending_commands WHERE id = ?",
                (dependency["start_command_id"],),
            ).fetchone()
            if start_row is None:
                self._drop_auto_break_start(source_id)
                continue
            newer_sendable_commands = self.connection.execute(
                "SELECT payload FROM pending_commands "
                "WHERE device_sequence > ? AND depends_on_command_id IS NULL",
                (int(start_row["device_sequence"]),),
            ).fetchall()
            if any(
                not isinstance(command, dict) or command.get("type") == "start"
                for command in (
                    json.loads(row["payload"]) for row in newer_sendable_commands
                )
            ):
                self._drop_auto_break_start(source_id)
                continue

            phase = next_break_phase(
                canonical["history"],
                completion.get("completedAt") or completion.get("endedAt"),
            )
            provisional_phase = self._transform_auto_break_chain(
                source_id,
                str(dependency["start_command_id"]),
                phase,
                canonical["durationsMs"][phase],
            )
            if provisional_phase is None:
                self._drop_auto_break_start(source_id)
                continue
            self.connection.execute(
                "UPDATE pending_commands SET depends_on_command_id = NULL "
                "WHERE depends_on_command_id = ?",
                (source_id,),
            )
            self.connection.execute(
                "DELETE FROM pending_auto_break_starts "
                "WHERE source_finish_command_id = ?",
                (source_id,),
            )
            self.connection.execute(
                "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
                (source_id,),
            )
            settings = self._normalize_settings(self.get_meta("settings", {}))
            if (
                settings["selectedPhase"] == provisional_phase
                and int(self.get_meta("selectedPhaseVersion", 0))
                == int(dependency["selected_phase_version"])
            ):
                settings["selectedPhase"] = phase
                self._set_meta("settings", settings)

    def _reconcile_selected_phase_advances(
        self,
        canonical: dict[str, Any],
        discarded_command_ids: set[str] | None = None,
    ) -> None:
        acknowledgements = {
            acknowledgement["commandId"]: acknowledgement
            for acknowledgement in canonical["acknowledgements"]
        }
        discarded_command_ids = discarded_command_ids or set()
        canonical_timer = canonical["canonicalTimer"]
        rows = self.connection.execute(
            "SELECT finish_command_id, timer_id, source_phase, advanced_phase, "
            "selected_phase_version FROM pending_phase_advances"
        ).fetchall()
        for row in rows:
            finish_id = str(row["finish_command_id"])
            acknowledgement = acknowledgements.get(finish_id)
            discarded = finish_id in discarded_command_ids
            if acknowledgement is None and not discarded:
                continue
            exact_completion = any(
                item.get("timerId") == row["timer_id"]
                and item.get("commandId") == finish_id
                and item.get("phase") == row["source_phase"]
                and item.get("status") == "completed"
                for item in canonical["history"]
            ) or (
                canonical_timer is not None
                and canonical_timer.get("id") == row["timer_id"]
                and canonical_timer.get("phase") == row["source_phase"]
                and canonical_timer.get("status") == "completed"
                and isinstance(canonical_timer.get("lastIntent"), dict)
                and canonical_timer["lastIntent"].get("commandId") == finish_id
            )
            non_applied = discarded or (
                acknowledgement is not None
                and acknowledgement["outcome"] != "applied"
            )
            if non_applied and not exact_completion:
                settings = self._normalize_settings(self.get_meta("settings", {}))
                if (
                    settings["selectedPhase"] == row["advanced_phase"]
                    and int(self.get_meta("selectedPhaseVersion", 0))
                    == int(row["selected_phase_version"])
                ):
                    settings["selectedPhase"] = row["source_phase"]
                    self._set_meta("settings", settings)
            self.connection.execute(
                "DELETE FROM pending_phase_advances WHERE finish_command_id = ?",
                (finish_id,),
            )

    def _apply_acknowledgements(
        self, canonical: dict[str, Any], *, delete: bool = True
    ) -> list[str]:
        notices: list[str] = []
        groups = (
            (
                "acknowledgements",
                "DELETE FROM pending_commands WHERE id = ?",
                "commandId",
            ),
            (
                "taskAcknowledgements",
                "DELETE FROM pending_task_operations WHERE id = ?",
                "operationId",
            ),
            (
                "durationAcknowledgements",
                "DELETE FROM pending_duration_operations WHERE id = ?",
                "operationId",
            ),
            (
                "autoStartAcknowledgements",
                "DELETE FROM pending_auto_start_operations WHERE id = ?",
                "operationId",
            ),
            (
                "selectedTaskAcknowledgements",
                "DELETE FROM pending_selected_task_operations WHERE id = ?",
                "operationId",
            ),
        )
        for response_key, delete_statement, id_key in groups:
            for acknowledgement in canonical[response_key]:
                if delete:
                    self.connection.execute(
                        delete_statement,
                        (acknowledgement[id_key],),
                    )
                if acknowledgement["outcome"] != "applied":
                    notices.append(
                        acknowledgement["reason"] or acknowledgement["outcome"]
                    )
        return notices

    def _rebase_retained_operations(
        self,
        canonical: dict[str, Any],
        trusted_response_ms: int,
    ) -> None:
        minimum_ms = max(1, trusted_response_ms - MAX_CLOCK_SKEW_MS)
        maximum_ms = min(
            MAX_SAFE_INTEGER,
            trusted_response_ms + MAX_CLOCK_SKEW_MS,
        )
        canonical_clock = (
            canonical["serverHlcWallMs"],
            canonical["serverHlcCounter"],
        )
        if not minimum_ms <= canonical_clock[0] <= maximum_ms:
            raise ValueError("Canonical server clock leaves no safe rebase headroom.")
        pending = self._preflight_pending_queues(require_clock_coverage=False)
        if any(
            operation["hlcWallMs"] > maximum_ms
            for domain in (
                pending["commands"],
                pending["taskOperations"],
                pending["durationOperations"],
                pending["autoStartOperations"],
                pending["selectedTaskOperations"],
            )
            for operation in domain
            if operation["hlcWallMs"] > 0
        ):
            raise ValueError(
                "Retained pending operation exceeds the trusted-time limit."
            )

        def next_clock(cursor: tuple[int, int]) -> tuple[int, int]:
            wall_ms, counter = cursor
            if counter < MAX_SAFE_INTEGER:
                return wall_ms, counter + 1
            if wall_ms >= maximum_ms:
                raise ValueError(
                    "Retained operation clock has no safe rebase headroom."
                )
            return wall_ms + 1, 0

        def rebase_domain(
            operations: list[dict[str, Any]],
            sort_key: Any,
        ) -> dict[str, tuple[int, int]]:
            cursor = canonical_clock
            replacements: dict[str, tuple[int, int]] = {}
            for operation in sorted(operations, key=sort_key):
                clock = (
                    int(operation["hlcWallMs"]),
                    int(operation["hlcCounter"]),
                )
                can_remain = (
                    minimum_ms <= clock[0] <= maximum_ms
                    and clock > cursor
                )
                if can_remain:
                    cursor = clock
                    continue
                cursor = next_clock(cursor)
                replacements[str(operation["id"])] = cursor
            return replacements

        replacements = {
            "commands": rebase_domain(
                pending["commands"],
                lambda operation: (
                    operation["deviceSequence"],
                    operation["id"],
                ),
            ),
            "taskOperations": rebase_domain(
                pending["taskOperations"],
                lambda operation: (
                    operation["hlcWallMs"],
                    operation["hlcCounter"],
                    operation["id"],
                ),
            ),
            "durationOperations": rebase_domain(
                [
                    operation
                    for operation in pending["durationOperations"]
                    if operation["hlcWallMs"] > 0
                ],
                lambda operation: (
                    operation["hlcWallMs"],
                    operation["hlcCounter"],
                    operation["id"],
                ),
            ),
            "autoStartOperations": rebase_domain(
                [
                    operation
                    for operation in pending["autoStartOperations"]
                    if operation["hlcWallMs"] > 0
                ],
                lambda operation: (
                    operation["hlcWallMs"],
                    operation["hlcCounter"],
                    operation["id"],
                ),
            ),
            "selectedTaskOperations": rebase_domain(
                [
                    operation
                    for operation in pending["selectedTaskOperations"]
                    if operation["hlcWallMs"] > 0
                ],
                lambda operation: (
                    operation["hlcWallMs"],
                    operation["hlcCounter"],
                    operation["id"],
                ),
            ),
        }
        tables = {
            "commands": "pending_commands",
            "taskOperations": "pending_task_operations",
            "durationOperations": "pending_duration_operations",
            "autoStartOperations": "pending_auto_start_operations",
            "selectedTaskOperations": "pending_selected_task_operations",
        }
        operations_by_domain = {
            "commands": pending["commands"],
            "taskOperations": pending["taskOperations"],
            "durationOperations": pending["durationOperations"],
            "autoStartOperations": pending["autoStartOperations"],
            "selectedTaskOperations": pending["selectedTaskOperations"],
        }
        for domain, domain_replacements in replacements.items():
            if not domain_replacements:
                continue
            for operation in operations_by_domain[domain]:
                clock = domain_replacements.get(str(operation["id"]))
                if clock is None:
                    continue
                rebased = dict(operation)
                original_ms = parse_timestamp_ms(str(operation["occurredAt"]))
                if (
                    original_ms is None
                    or not minimum_ms <= original_ms <= maximum_ms
                    or abs(clock[0] - original_ms) > MAX_CLOCK_SKEW_MS
                ):
                    rebased["occurredAt"] = utc_timestamp(clock[0])
                rebased["hlcWallMs"], rebased["hlcCounter"] = clock
                self.connection.execute(
                    f"UPDATE {tables[domain]} SET payload = ? WHERE id = ?",
                    (
                        json.dumps(rebased, separators=(",", ":")),
                        rebased["id"],
                    ),
                )

    def _delete_resolution_queue_ids(self, queue_ids: dict[str, list[str]]) -> None:
        groups = (
            ("commands", "DELETE FROM pending_commands WHERE id = ?"),
            (
                "taskOperations",
                "DELETE FROM pending_task_operations WHERE id = ?",
            ),
            (
                "durationOperations",
                "DELETE FROM pending_duration_operations WHERE id = ?",
            ),
            (
                "autoStartOperations",
                "DELETE FROM pending_auto_start_operations WHERE id = ?",
            ),
            (
                "selectedTaskOperations",
                "DELETE FROM pending_selected_task_operations WHERE id = ?",
            ),
        )
        for key, statement in groups:
            if key not in queue_ids:
                continue
            self.connection.executemany(
                statement, ((item_id,) for item_id in queue_ids[key])
            )

    def _install_canonical(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        *,
        preserve_known_tasks: bool,
        clock_sample: dict[str, int] | None,
        trusted_response_ms: int,
    ) -> None:
        now_ms = self._physical_time_ms(trusted_response_ms)
        local_wall, local_counter = self._logical_clock(
            self.get_meta("hlc", {"wallMs": 0, "counter": 0}),
            allow_legacy_zero=True,
        )
        server_clock = (
            canonical["serverHlcWallMs"],
            canonical["serverHlcCounter"],
        )
        local_clock = (local_wall, local_counter)
        pending = self._preflight_pending_queues(require_clock_coverage=False)
        ownership = self.get_meta("centralizedTimerOwnership")
        owned_timer_id = (
            ownership.get("timerId") if isinstance(ownership, dict) else None
        )
        canonical_timer = canonical["canonicalTimer"]
        retained_timer_ids = {
            operation.get("timerId")
            for operation in pending["commands"]
            if operation.get("type") == "start"
        }
        if owned_timer_id is not None and (
            not isinstance(canonical_timer, dict)
            or canonical_timer.get("id") != owned_timer_id
        ) and owned_timer_id not in retained_timer_ids:
            self._set_meta("centralizedTimerOwnership", None)
        retained_clocks = [
            (operation["hlcWallMs"], operation["hlcCounter"])
            for operations in (
                pending["commands"],
                pending["taskOperations"],
                pending["durationOperations"],
                pending["autoStartOperations"],
                pending["selectedTaskOperations"],
            )
            for operation in operations
            if operation["hlcWallMs"] > 0
        ]
        retained_clock = max(retained_clocks, default=(0, 0))
        if retained_clock[0] - now_ms > MAX_CLOCK_SKEW_MS:
            raise ValueError(
                "Retained pending operation exceeds the trusted-time limit."
            )
        candidates = [server_clock, retained_clock]
        if local_wall > 0 and local_wall - now_ms <= MAX_CLOCK_SKEW_MS:
            candidates.append(local_clock)
        merged_wall, merged_counter = max(candidates)

        settings = self._normalize_settings(self.get_meta("settings", {}))
        pending_duration_operations = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_duration_operations"
            )
        ]
        settings["durationsMs"] = project_durations(
            canonical["durationsMs"], pending_duration_operations
        )
        settings["durations"] = {
            phase: self._display_minutes(duration_ms)
            for phase, duration_ms in settings["durationsMs"].items()
        }
        settings["autoStartBreaks"] = project_auto_start_breaks(
            canonical["autoStartBreaks"], self._pending_auto_start_operations()
        )
        settings["selectedTaskId"] = canonical["selectedTaskId"]
        pending_selected_tasks = self._pending_selected_task_operations()
        if pending_selected_tasks:
            settings["selectedTaskId"] = pending_selected_tasks[-1]["taskId"]
        self._set_meta("settings", settings)

        previous = self.get_meta("snapshot", {})
        known = (
            {
                task["id"]: task
                for task in previous.get("knownTasks", [])
                if task.get("id") and task.get("title")
            }
            if preserve_known_tasks
            else {}
        )
        for task in canonical["tasks"]:
            if task.get("id") and task.get("title"):
                known[task["id"]] = task
        self._set_meta(
            "snapshot",
            {
                "revision": canonical["revision"],
                "canonicalTimer": canonical["canonicalTimer"],
                "history": canonical["history"],
                "tasks": canonical["tasks"],
                "knownTasks": sorted(
                    known.values(),
                    key=lambda item: (item["title"].casefold(), item["id"]),
                ),
                "autoStartBreaks": canonical["autoStartBreaks"],
                "selectedTaskId": canonical["selectedTaskId"],
                "user": user,
            },
        )
        self._set_meta("autoStartLegacyDefaultUnknown", False)
        self._set_meta("hlc", {"wallMs": merged_wall, "counter": merged_counter})
        if clock_sample is not None:
            self._set_meta("serverClockSample", clock_sample)

    def apply_sync(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            claimed = self.pending_sync()
            if claimed != request:
                raise ValueError(
                    "Sync response did not match an active normal sync claim."
                )
            previous = self.get_meta("snapshot")
            canonical = self._validated_sync_response(response, request)
            clock_sample, clock_anchor = self._clock_sample_for_response(
                canonical["serverTimeMs"],
                request_physical_ms,
                received_physical_ms,
                request_monotonic_ms,
                received_monotonic_ms,
            )
            trusted_response_ms = (
                canonical["serverTimeMs"]
                if clock_anchor is None
                else clock_anchor["acquiredTrustedMs"]
            )
            if canonical["revision"] < int(previous.get("revision", 0)):
                raise ValueError("Server response would regress canonical revision.")
            self._reconcile_selected_phase_advances(canonical)
            self._resolve_auto_break_dependencies(canonical)
            notices = self._apply_acknowledgements(canonical)
            self._rebase_retained_operations(canonical, trusted_response_ms)
            self._install_canonical(
                canonical,
                previous.get("user"),
                preserve_known_tasks=True,
                clock_sample=clock_sample,
                trusted_response_ms=trusted_response_ms,
            )
            self._prune_command_physical_times()
            if claimed is not None:
                self._set_meta("pendingSync", None)
        if clock_anchor is not None:
            self._set_trusted_time_anchor(clock_anchor)
        return notices

    def apply_resolution(
        self,
        response: dict[str, Any],
        user: dict[str, Any],
        request_id: str | None = None,
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        user_id = user.get("id") if isinstance(user, dict) else None
        with self._immediate_transaction():
            pending = self.pending_resolution(user_id)
            if pending is None:
                raise ValueError("No matching history resolution is pending.")
            request = pending["request"]
            if request_id is not None and request.get("requestId") != request_id:
                raise ValueError("History resolution response matched a stale request.")
            canonical = self._validated_sync_response(response, request)
            clock_sample, clock_anchor = self._clock_sample_for_response(
                canonical["serverTimeMs"],
                request_physical_ms,
                received_physical_ms,
                request_monotonic_ms,
                received_monotonic_ms,
            )
            trusted_response_ms = (
                canonical["serverTimeMs"]
                if clock_anchor is None
                else clock_anchor["acquiredTrustedMs"]
            )
            previous = self.get_meta("snapshot", {})
            minimum_revision = max(
                int(previous.get("revision", 0)), int(request["expectedRevision"])
            )
            if canonical["revision"] < minimum_revision:
                raise ValueError("Server response would regress canonical revision.")
            strategy = request.get("strategy")
            if strategy not in {"keep_remote", "replace_remote", "merge"}:
                raise ValueError("Pending history resolution has an invalid strategy.")
            queue_ids = pending["queueIds"]
            request_ids = {
                key: [item.get("id") for item in request.get(key, [])]
                for key in queue_ids
            }
            if strategy == "keep_remote":
                if any(request_ids.values()):
                    raise ValueError("Keep-remote resolution contains local operations.")
            elif request_ids != queue_ids:
                raise ValueError(
                    "Pending history resolution does not match captured queue IDs."
                )
            self._reconcile_selected_phase_advances(
                canonical,
                set(queue_ids["commands"]) if strategy == "keep_remote" else None,
            )
            self._resolve_auto_break_dependencies(canonical)
            notices = self._apply_acknowledgements(canonical, delete=False)
            self._delete_resolution_queue_ids(queue_ids)
            self._rebase_retained_operations(canonical, trusted_response_ms)
            if strategy == "keep_remote" and "autoStartOperations" not in queue_ids:
                self.connection.execute("DELETE FROM pending_auto_start_operations")
            if strategy == "keep_remote" and "selectedTaskOperations" not in queue_ids:
                self.connection.execute("DELETE FROM pending_selected_task_operations")
            if strategy == "keep_remote":
                self.connection.execute("DELETE FROM pending_auto_breaks")
                self.connection.execute("DELETE FROM pending_auto_break_starts")
            self._install_canonical(
                canonical,
                user,
                preserve_known_tasks=strategy != "keep_remote",
                clock_sample=clock_sample,
                trusted_response_ms=trusted_response_ms,
            )
            self._prune_command_physical_times()
            self._set_meta("pendingResolution", None)
        if clock_anchor is not None:
            self._set_trusted_time_anchor(clock_anchor)
        return notices

    def set_user(self, user: dict[str, Any] | None) -> None:
        with self._immediate_transaction():
            snapshot = self.get_meta("snapshot")
            snapshot["user"] = user
            self._set_meta("snapshot", snapshot)

    @property
    def replication_mode(self) -> str:
        mode = self.get_meta("replicationMode", "centralized")
        return mode if mode in {"offline", "iroh", "centralized"} else "centralized"

    @property
    def active_iroh_room_id(self) -> str | None:
        room_id = self.get_meta("activeIrohRoomId")
        return room_id if isinstance(room_id, str) and room_id else None

    def _capture_workspace(self) -> dict[str, Any]:
        metadata = {
            key: self.get_meta(key)
            for key in (
                "settings",
                "snapshot",
                "deviceSequence",
                "hlc",
                "lastUuidV7",
                "serverClockSample",
                "commandPhysicalTimes",
                "selectedPhaseVersion",
                "autoStartLegacyDefaultUnknown",
                "pendingSync",
                "pendingResolution",
            )
        }
        tables = {}
        for table, columns in (
            (
                "pending_commands",
                ("id", "device_sequence", "payload", "depends_on_command_id"),
            ),
            ("pending_task_operations", ("id", "payload")),
            ("pending_duration_operations", ("id", "phase", "payload")),
            ("pending_auto_start_operations", ("id", "payload")),
            ("pending_selected_task_operations", ("id", "payload")),
            (
                "pending_auto_breaks",
                ("finish_command_id", "timer_id", "finish_device_sequence"),
            ),
            (
                "pending_auto_break_starts",
                (
                    "source_finish_command_id",
                    "source_timer_id",
                    "start_command_id",
                    "selected_phase_version",
                ),
            ),
            (
                "pending_phase_advances",
                (
                    "finish_command_id",
                    "timer_id",
                    "source_phase",
                    "advanced_phase",
                    "selected_phase_version",
                ),
            ),
        ):
            tables[table] = [
                {column: row[column] for column in columns}
                for row in self.connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
                )
            ]
        return {"metadata": metadata, "tables": tables}

    def _restore_workspace(self, workspace: dict[str, Any]) -> None:
        if (
            not isinstance(workspace, dict)
            or not isinstance(workspace.get("metadata"), dict)
            or not isinstance(workspace.get("tables"), dict)
        ):
            raise ValueError("Saved replication workspace is invalid.")
        table_columns = {
            "pending_commands": (
                "id",
                "device_sequence",
                "payload",
                "depends_on_command_id",
            ),
            "pending_task_operations": ("id", "payload"),
            "pending_duration_operations": ("id", "phase", "payload"),
            "pending_auto_start_operations": ("id", "payload"),
            "pending_selected_task_operations": ("id", "payload"),
            "pending_auto_breaks": (
                "finish_command_id",
                "timer_id",
                "finish_device_sequence",
            ),
            "pending_auto_break_starts": (
                "source_finish_command_id",
                "source_timer_id",
                "start_command_id",
                "selected_phase_version",
            ),
            "pending_phase_advances": (
                "finish_command_id",
                "timer_id",
                "source_phase",
                "advanced_phase",
                "selected_phase_version",
            ),
        }
        for table in table_columns:
            self.connection.execute(f"DELETE FROM {table}")
        for table, columns in table_columns.items():
            rows = workspace["tables"].get(table, [])
            if not isinstance(rows, list):
                raise ValueError("Saved replication queue is invalid.")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ValueError("Saved replication queue row is invalid.")
                self.connection.execute(
                    f"INSERT INTO {table}({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
        for key, value in workspace["metadata"].items():
            if key in {
                "settings",
                "snapshot",
                "deviceSequence",
                "hlc",
                "lastUuidV7",
                "serverClockSample",
                "commandPhysicalTimes",
                "selectedPhaseVersion",
                "autoStartLegacyDefaultUnknown",
                "pendingSync",
                "pendingResolution",
            }:
                self._set_meta(key, value)

    def _workspace_without_account(
        self, workspace: dict[str, Any], *, preserve_domain: bool
    ) -> dict[str, Any]:
        cleared = deepcopy(workspace)
        metadata = cleared["metadata"]
        settings = self._normalize_settings(metadata.get("settings", {}))
        snapshot = deepcopy(metadata.get("snapshot", {}))
        snapshot["revision"] = 0
        snapshot["user"] = None
        metadata.update(
            pendingSync=None,
            pendingResolution=None,
            serverClockSample=None,
            commandPhysicalTimes={},
        )
        if preserve_domain:
            metadata["snapshot"] = snapshot
            return cleared

        settings["durations"] = {
            phase: int(definition["default_minutes"])
            for phase, definition in PHASES.items()
        }
        settings["durationsMs"] = {
            phase: int(definition["default_minutes"]) * 60_000
            for phase, definition in PHASES.items()
        }
        settings["selectedTaskId"] = None
        settings["autoStartBreaks"] = False
        metadata["settings"] = settings
        metadata["snapshot"] = {
            "revision": 0,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "knownTasks": [],
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "user": None,
        }
        metadata["autoStartLegacyDefaultUnknown"] = False
        for table in cleared["tables"]:
            cleared["tables"][table] = []
        return cleared

    def _projected_local_genesis(self) -> dict[str, Any]:
        state = self.load()
        snapshot = state["snapshot"]
        timer, history = rebuild_optimistic(
            snapshot.get("canonicalTimer"),
            snapshot.get("history", []),
            state["pending"],
        )
        tasks = rebuild_tasks(snapshot.get("tasks", []), state["pendingTasks"])
        durations = project_durations(
            self._normalize_settings(state["settings"])["durationsMs"],
            state["pendingDurations"],
        )
        auto_start = project_auto_start_breaks(
            bool(snapshot.get("autoStartBreaks", False)), state["pendingAutoStarts"]
        )
        selected_task_id = state["settings"].get("selectedTaskId")
        clocks = [
            (int(operation.get("hlcWallMs", 0)), int(operation.get("hlcCounter", 0)))
            for operations in (
                state["pending"],
                state["pendingTasks"],
                state["pendingDurations"],
                state["pendingAutoStarts"],
                state["pendingSelectedTasks"],
            )
            for operation in operations
        ]
        clocks.append(
            self._logical_clock(
                self.get_meta("hlc", {"wallMs": 0, "counter": 0}),
                allow_legacy_zero=True,
            )
        )
        wall, counter = max(clocks, default=(0, 0))
        clean_history = []
        for item in history:
            cleaned = deepcopy(item)
            cleaned.pop("pending", None)
            cleaned["id"] = cleaned["timerId"]
            for key in ("commandId", "taskId", "completedAt", "endedAt"):
                if cleaned.get(key) is None:
                    cleaned.pop(key, None)
            clean_history.append(cleaned)
        clean_history.sort(key=self._history_order)
        clean_timer = deepcopy(timer)
        if clean_timer is not None:
            for key in ("taskId", "startedByDeviceId", "lastIntent"):
                if clean_timer.get(key) is None:
                    clean_timer.pop(key, None)
            intent = clean_timer.get("lastIntent")
            if isinstance(intent, dict):
                intent.pop("deviceId", None)
        return {
            "canonicalTimer": clean_timer,
            "history": clean_history,
            "tasks": tasks,
            "durationsMs": durations,
            "autoStartBreaks": auto_start,
            "selectedTaskId": selected_task_id,
            "hlcWallMs": wall,
            "hlcCounter": counter,
        }

    @staticmethod
    def _history_order(item: dict[str, Any]) -> tuple[int, bytes]:
        timestamp = item.get("endedAt") or item.get("completedAt")
        milliseconds = parse_timestamp_ms(timestamp) if isinstance(timestamp, str) else None
        return (-(milliseconds or 0), str(item.get("timerId", "")).encode("utf-8"))

    def _empty_iroh_workspace(self, genesis: dict[str, Any]) -> dict[str, Any]:
        workspace = self._capture_workspace()
        settings = self._normalize_settings(workspace["metadata"]["settings"])
        settings["durationsMs"] = deepcopy(genesis["durationsMs"])
        settings["durations"] = {
            phase: self._display_minutes(duration)
            for phase, duration in genesis["durationsMs"].items()
        }
        settings["autoStartBreaks"] = genesis["autoStartBreaks"]
        settings["selectedTaskId"] = genesis["selectedTaskId"]
        workspace["metadata"].update(
            settings=settings,
            snapshot={
                "revision": 0,
                "canonicalTimer": deepcopy(genesis["canonicalTimer"]),
                "history": deepcopy(genesis["history"]),
                "tasks": sorted(
                    deepcopy(genesis["tasks"]),
                    key=lambda item: (item["title"].encode(), item["id"].encode()),
                ),
                "knownTasks": sorted(
                    deepcopy(genesis["tasks"]),
                    key=lambda item: (item["title"].encode(), item["id"].encode()),
                ),
                "autoStartBreaks": genesis["autoStartBreaks"],
                "selectedTaskId": settings["selectedTaskId"],
                "user": None,
            },
            hlc={"wallMs": genesis["hlcWallMs"], "counter": genesis["hlcCounter"]},
            serverClockSample=None,
            commandPhysicalTimes={},
            pendingSync=None,
            pendingResolution=None,
        )
        for table in workspace["tables"]:
            workspace["tables"][table] = []
        return workspace

    def create_iroh_room(
        self,
        room_secret: bytes,
        room_name: str | None = None,
        *,
        now_ms: int | None = None,
    ) -> str:
        from .iroh_protocol import (
            IrohProtocolError,
            record_digest,
            room_id_for_secret,
            validate_record,
        )

        if room_name is not None and not 1 <= len(room_name) <= 64:
            raise ValueError("Room name must contain 1 through 64 Unicode scalar values.")
        room_id = room_id_for_secret(room_secret)
        genesis = self._projected_local_genesis()
        record = {
            "domain": "genesis",
            "deviceId": self.device_id,
            "operation": genesis,
        }
        try:
            validate_record(record)
            digest = record_digest(record)
        except IrohProtocolError as error:
            raise ValueError(str(error)) from error
        return_workspace = self._capture_workspace()
        room_workspace = self._empty_iroh_workspace(genesis)
        created_at = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            if self.connection.execute(
                "SELECT 1 FROM iroh_rooms WHERE room_id = ?", (room_id,)
            ).fetchone():
                raise ValueError("An Iroh room with this identity already exists.")
            self.connection.execute(
                "INSERT INTO iroh_rooms(room_id, room_secret, room_name, "
                "return_workspace, workspace, created_at_ms, conflict) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    room_id,
                    self._secure_reference(self._room_secret_key(room_id)),
                    room_name,
                    json.dumps(return_workspace, separators=(",", ":")),
                    json.dumps(room_workspace, separators=(",", ":")),
                    created_at,
                ),
            )
            self.connection.execute(
                "INSERT INTO iroh_records(room_id, domain, operation_id, device_id, digest, record) "
                "VALUES (?, 'genesis', 'genesis', ?, ?, ?)",
                (
                    room_id,
                    self.device_id,
                    digest,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._restore_workspace(room_workspace)
            self._set_meta("activeIrohRoomId", room_id)
            self._set_meta("replicationMode", "iroh")
            try:
                self._iroh_secret_store.save(self._room_secret_key(room_id), room_secret)
            except Exception:
                self.connection.execute("DELETE FROM iroh_rooms WHERE room_id = ?", (room_id,))
                raise
        return room_id

    def prepare_iroh_join(
        self,
        room_id: str,
        room_secret: bytes,
        room_name: str | None,
        endpoint_id: str,
        endpoint_ticket: str,
        *,
        now_ms: int | None = None,
    ) -> None:
        from .iroh_protocol import room_id_for_secret, valid_room_id

        if (
            not valid_room_id(room_id)
            or room_id_for_secret(room_secret) != room_id
            or room_name is not None
            and not 1 <= len(room_name) <= 64
        ):
            raise ValueError("Iroh room metadata is invalid.")
        created_at = now_ms if now_ms is not None else int(time.time() * 1000)
        return_workspace = self._capture_workspace()
        room_workspace = self._empty_iroh_workspace(
            {
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": self._normalize_settings(
                    return_workspace["metadata"]["settings"]
                )["durationsMs"],
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "hlcWallMs": 0,
                "hlcCounter": 0,
            }
        )
        with self._immediate_transaction():
            existing = self.connection.execute(
                "SELECT conflict FROM iroh_rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if existing is not None:
                saved = self.iroh_room_secret(room_id)
                if saved != room_secret:
                    raise ValueError("Saved Iroh room credentials do not match invite.")
                if existing["conflict"] is not None:
                    raise ValueError("Saved Iroh room requires immutable-conflict repair.")
                self._upsert_iroh_peer(
                    room_id, endpoint_id, endpoint_ticket, None, None, None
                )
                return
            self.connection.execute(
                "INSERT INTO iroh_rooms(room_id, room_secret, room_name, "
                "return_workspace, workspace, created_at_ms, conflict) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    room_id,
                    self._secure_reference(self._room_secret_key(room_id)),
                    room_name,
                    json.dumps(return_workspace, separators=(",", ":")),
                    json.dumps(room_workspace, separators=(",", ":")),
                    created_at,
                ),
            )
            self._upsert_iroh_peer(
                room_id, endpoint_id, endpoint_ticket, None, None, None
            )
            try:
                self._iroh_secret_store.save(self._room_secret_key(room_id), room_secret)
            except Exception:
                self.connection.execute("DELETE FROM iroh_rooms WHERE room_id = ?", (room_id,))
                raise

    def discard_inactive_iroh_room(self, room_id: str) -> None:
        with self._immediate_transaction():
            if self.active_iroh_room_id == room_id:
                raise ValueError("Active Iroh room cannot be discarded.")
            conflict = self.connection.execute(
                "SELECT conflict FROM iroh_rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            if conflict is not None and conflict["conflict"] is not None:
                return
            peers = self.connection.execute(
                "SELECT endpoint_id FROM iroh_peers WHERE room_id = ?", (room_id,)
            ).fetchall()
            self.connection.execute("DELETE FROM iroh_rooms WHERE room_id = ?", (room_id,))
            self._iroh_secret_store.delete(self._room_secret_key(room_id))
            for peer in peers:
                self._iroh_secret_store.delete(
                    self._peer_ticket_key(room_id, str(peer["endpoint_id"]))
                )

    def activate_joined_iroh_room(self, room_id: str) -> None:
        with self._immediate_transaction():
            room = self.connection.execute(
                "SELECT workspace, conflict FROM iroh_rooms WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            genesis = self.connection.execute(
                "SELECT 1 FROM iroh_records WHERE room_id = ? AND domain = 'genesis' "
                "AND operation_id = 'genesis'",
                (room_id,),
            ).fetchone()
            if room is None or genesis is None or room["conflict"] is not None:
                raise ValueError("Joined Iroh room has no valid genesis or requires repair.")
            projection = self._project_iroh_room(room_id)
            return_workspace = self._capture_workspace()
            workspace = self._workspace_with_iroh_projection(
                json.loads(room["workspace"]), projection
            )
            self.connection.execute(
                "UPDATE iroh_rooms SET return_workspace = ?, workspace = ? WHERE room_id = ?",
                (
                    json.dumps(return_workspace, separators=(",", ":")),
                    json.dumps(workspace, separators=(",", ":")),
                    room_id,
                ),
            )
            self._restore_workspace(workspace)
            self._set_meta("activeIrohRoomId", room_id)
            self._set_meta("replicationMode", "iroh")

    def set_replication_mode(self, mode: str) -> None:
        if mode not in {"offline", "iroh", "centralized"}:
            raise ValueError("Replication mode must be offline, iroh, or centralized.")
        with self._immediate_transaction():
            current = self.replication_mode
            if current == mode:
                return
            if current == "iroh":
                room_id = self.active_iroh_room_id
                if room_id is None:
                    raise ValueError("Active Iroh room metadata is missing.")
                room = self.connection.execute(
                    "SELECT return_workspace, conflict FROM iroh_rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if room is None:
                    raise ValueError("Active Iroh room workspace is missing.")
                if room["conflict"] is None:
                    self._capture_local_iroh_records_locked(room_id)
                else:
                    self.connection.execute(
                        "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                        (
                            json.dumps(self._capture_workspace(), separators=(",", ":")),
                            room_id,
                        ),
                    )
                self._restore_workspace(json.loads(room["return_workspace"]))
                self._set_meta("activeIrohRoomId", None)
            if mode == "iroh":
                room = self.connection.execute(
                    "SELECT room_id, workspace, conflict FROM iroh_rooms "
                    "WHERE EXISTS (SELECT 1 FROM iroh_records AS records "
                    "WHERE records.room_id = iroh_rooms.room_id "
                    "AND records.domain = 'genesis' AND records.operation_id = 'genesis') "
                    "ORDER BY created_at_ms DESC LIMIT 1"
                ).fetchone()
                if room is None:
                    raise ValueError("Create or join an Iroh room before selecting Iroh mode.")
                if room["conflict"] is not None:
                    raise ValueError("Saved Iroh room requires repair before activation.")
                self.connection.execute(
                    "UPDATE iroh_rooms SET return_workspace = ? WHERE room_id = ?",
                    (
                        json.dumps(self._capture_workspace(), separators=(",", ":")),
                        room["room_id"],
                    ),
                )
                self._restore_workspace(json.loads(room["workspace"]))
                self._set_meta("activeIrohRoomId", str(room["room_id"]))
            self._set_meta("replicationMode", mode)

    def leave_iroh_room(self) -> None:
        if self.replication_mode != "iroh":
            raise ValueError("No Iroh room is active.")
        self.set_replication_mode("offline")

    def iroh_room(self, room_id: str | None = None) -> dict[str, Any] | None:
        room_id = room_id or self.active_iroh_room_id
        if room_id is None:
            return None
        row = self.connection.execute(
            "SELECT room_id, room_name, created_at_ms, conflict FROM iroh_rooms "
            "WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        peer_count = int(
            self.connection.execute(
                "SELECT COUNT(*) AS count FROM iroh_peers WHERE room_id = ?",
                (room_id,),
            ).fetchone()["count"]
        )
        operation_count = int(
            self.connection.execute(
                "SELECT COUNT(*) AS count FROM iroh_records WHERE room_id = ?",
                (room_id,),
            ).fetchone()["count"]
        )
        return {
            "roomId": str(row["room_id"]),
            "roomName": row["room_name"],
            "createdAtMs": int(row["created_at_ms"]),
            "peerCount": peer_count,
            "operationCount": operation_count,
            "conflict": json.loads(row["conflict"]) if row["conflict"] else None,
        }

    def iroh_room_secret(self, room_id: str) -> bytes:
        row = self.connection.execute(
            "SELECT room_secret FROM iroh_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Saved Iroh room secret is unavailable or invalid.")
        secret = self._iroh_secret_store.load(self._room_secret_key(room_id))
        if secret is None or len(secret) != 32:
            raise ValueError("Saved Iroh room secret is unavailable or invalid.")
        return secret

    def capture_local_iroh_records(self) -> bool:
        room_id = self.active_iroh_room_id
        if self.replication_mode != "iroh" or room_id is None:
            return False
        with self._immediate_transaction():
            return self._capture_local_iroh_records_locked(room_id)

    def _capture_iroh_after_mutation(self) -> None:
        with self._immediate_transaction():
            self._capture_iroh_after_mutation_locked()

    def _capture_iroh_after_mutation_locked(self) -> None:
        if self.replication_mode == "iroh":
            room = self.iroh_room()
            if room is not None and room.get("conflict") is not None:
                self.connection.execute(
                    "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                    (
                        json.dumps(self._capture_workspace(), separators=(",", ":")),
                        room["roomId"],
                    ),
                )
                return
            self._capture_local_iroh_records_locked(room["roomId"])

    def _capture_local_iroh_records_locked(self, room_id: str) -> bool:
        pending = self._preflight_pending_queues()
        records = []
        for domain, operations in (
            ("timer", pending["commands"]),
            ("task", pending["taskOperations"]),
            ("duration", pending["durationOperations"]),
            ("autoStart", pending["autoStartOperations"]),
            ("selectedTask", pending["selectedTaskOperations"]),
        ):
            for operation in operations:
                wire_operation = deepcopy(operation)
                if domain in {"autoStart", "selectedTask"}:
                    wire_operation.pop("deviceId", None)
                records.append(
                    {
                        "domain": domain,
                        "deviceId": self.device_id,
                        "operation": wire_operation,
                    }
                )
        if not records:
            self.connection.execute(
                "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                (json.dumps(self._capture_workspace(), separators=(",", ":")), room_id),
            )
            return False
        self._insert_iroh_records_locked(room_id, records)
        command_ids = [record["operation"]["id"] for record in records if record["domain"] == "timer"]
        self.connection.execute("DELETE FROM pending_commands")
        self.connection.execute("DELETE FROM pending_task_operations")
        self.connection.execute("DELETE FROM pending_duration_operations")
        self.connection.execute("DELETE FROM pending_auto_start_operations")
        self.connection.execute("DELETE FROM pending_selected_task_operations")
        self.connection.executemany(
            "DELETE FROM pending_phase_advances WHERE finish_command_id = ?",
            ((identifier,) for identifier in command_ids),
        )
        self.connection.execute("DELETE FROM pending_auto_break_starts")
        self._set_meta("commandPhysicalTimes", {})
        self._set_meta("pendingSync", None)
        projection = self._project_iroh_room(room_id)
        room = self.connection.execute(
            "SELECT workspace FROM iroh_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if room is None:
            raise ValueError("Active Iroh room workspace is missing.")
        workspace = self._workspace_with_iroh_projection(
            self._capture_workspace(), projection
        )
        self.connection.execute(
            "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
            (json.dumps(workspace, separators=(",", ":")), room_id),
        )
        self._restore_workspace(workspace)
        return True

    def _workspace_with_iroh_projection(
        self, workspace: dict[str, Any], projection: dict[str, Any]
    ) -> dict[str, Any]:
        settings = self._normalize_settings(workspace["metadata"]["settings"])
        settings["durationsMs"] = deepcopy(projection["durationsMs"])
        settings["durations"] = {
            phase: self._display_minutes(duration)
            for phase, duration in projection["durationsMs"].items()
        }
        settings["autoStartBreaks"] = projection["autoStartBreaks"]
        settings["selectedTaskId"] = projection["selectedTaskId"]
        previous = workspace["metadata"].get("snapshot", {})
        known = {
            item["id"]: item
            for item in previous.get("knownTasks", [])
            if isinstance(item, dict) and item.get("id") and item.get("title")
        }
        known.update(
            {
                item["id"]: item
                for item in projection.get("knownTasks", projection["tasks"])
            }
        )
        workspace["metadata"].update(
            settings=settings,
            snapshot={
                "revision": 0,
                "canonicalTimer": projection["canonicalTimer"],
                "history": projection["history"],
                "tasks": projection["tasks"],
                "knownTasks": sorted(
                    known.values(),
                    key=lambda item: (item["title"].encode(), item["id"].encode()),
                ),
                "autoStartBreaks": projection["autoStartBreaks"],
                "selectedTaskId": settings["selectedTaskId"],
                "user": None,
            },
            hlc={"wallMs": projection["hlcWallMs"], "counter": projection["hlcCounter"]},
            serverClockSample=None,
            commandPhysicalTimes={},
            pendingSync=None,
            pendingResolution=None,
        )
        for table in (
            "pending_commands",
            "pending_task_operations",
            "pending_duration_operations",
            "pending_auto_start_operations",
            "pending_selected_task_operations",
            "pending_auto_break_starts",
            "pending_phase_advances",
        ):
            workspace["tables"][table] = []
        return workspace

    def _project_iroh_room(
        self, room_id: str, *, now_ms: int | None = None
    ) -> dict[str, Any]:
        from .iroh_protocol import operation_order, validate_record

        rows = self.connection.execute(
            "SELECT record FROM iroh_records WHERE room_id = ?", (room_id,)
        ).fetchall()
        records = [json.loads(row["record"]) for row in rows]
        for record in records:
            validate_record(record)
        genesis_records = [record for record in records if record["domain"] == "genesis"]
        if len(genesis_records) != 1:
            raise ValueError("Iroh room genesis is missing or conflicting.")
        genesis_record = genesis_records[0]
        genesis = deepcopy(genesis_record["operation"])
        timer = genesis["canonicalTimer"]
        history = genesis["history"]
        tasks = genesis["tasks"]
        durations = genesis["durationsMs"]
        auto_start = genesis["autoStartBreaks"]
        selected_task_id = genesis["selectedTaskId"]
        clocks = [(genesis["hlcWallMs"], genesis["hlcCounter"])]
        known_tasks = {task["id"]: task for task in genesis["tasks"]}
        timer_starters = {
            item["timerId"]: genesis_record["deviceId"]
            for item in genesis["history"]
        }
        if timer is not None:
            timer_starters[timer["id"]] = timer.get(
                "startedByDeviceId", genesis_record["deviceId"]
            )
        for record in sorted(
            (record for record in records if record["domain"] != "genesis"),
            key=operation_order,
        ):
            operation = record["operation"]
            clocks.append((operation["hlcWallMs"], operation["hlcCounter"]))
            if record["domain"] == "timer":
                timer, history = reduce_command(timer, history, operation)
                if (
                    operation.get("type") == "start"
                    and timer
                    and timer.get("id") == operation.get("timerId")
                    and isinstance(timer.get("lastIntent"), dict)
                    and timer["lastIntent"].get("commandId") == operation["id"]
                ):
                    timer_starters[operation["timerId"]] = record["deviceId"]
                if timer:
                    timer["startedByDeviceId"] = timer_starters.get(timer["id"])
                if (
                    timer
                    and isinstance(timer.get("lastIntent"), dict)
                    and timer["lastIntent"].get("commandId") == operation["id"]
                ):
                    timer["lastIntent"]["deviceId"] = record["deviceId"]
            elif record["domain"] == "task":
                tasks = rebuild_tasks(tasks, [operation])
                if operation.get("type") == "upsert":
                    try:
                        known = task_from_title(operation.get("title", ""))
                    except ValueError:
                        known = None
                    if known is not None and known["id"] == operation.get("taskId"):
                        known_tasks[known["id"]] = known
            elif record["domain"] == "duration":
                durations = project_durations(durations, [operation])
            elif record["domain"] == "autoStart":
                projected_operation = {**operation, "deviceId": record["deviceId"]}
                auto_start = project_auto_start_breaks(auto_start, [projected_operation])
            elif record["domain"] == "selectedTask":
                selected_task_id = operation["taskId"]
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if (
            timer is not None
            and timer.get("status") == "running"
            and elapsed_ms(timer, now_ms) >= int(timer["plannedDurationMs"])
        ):
            anchor_ms = parse_timestamp_ms(timer.get("anchorAt"))
            if anchor_ms is None:
                raise ValueError("Iroh room timer anchor is invalid.")
            completed_ms = (
                anchor_ms
                + int(timer["plannedDurationMs"])
                - int(timer.get("elapsedAtAnchorMs", 0))
            )
            completed_at = utc_timestamp(completed_ms)
            timer["status"] = "completed"
            timer["elapsedAtAnchorMs"] = int(timer["plannedDurationMs"])
            timer["anchorAt"] = completed_at
            if not any(item.get("timerId") == timer["id"] for item in history):
                completion = {
                    "id": timer["id"],
                    "timerId": timer["id"],
                    "phase": timer["phase"],
                    "status": "completed",
                    "plannedDurationMs": timer["plannedDurationMs"],
                    "completedAt": completed_at,
                    "endedAt": completed_at,
                }
                if timer.get("taskId") is not None:
                    completion["taskId"] = timer["taskId"]
                history.append(completion)
        for item in history:
            if item.get("timerId"):
                item["id"] = item["timerId"]
        clean_history = []
        for item in history:
            cleaned = deepcopy(item)
            cleaned.pop("pending", None)
            for key in ("commandId", "taskId", "completedAt", "endedAt"):
                if cleaned.get(key) is None:
                    cleaned.pop(key, None)
            clean_history.append(cleaned)
        clean_history.sort(key=self._history_order)
        if timer is not None and not self._valid_canonical_timer(timer):
            raise ValueError("Iroh room projected an invalid canonical timer.")
        if any(not self._valid_history_item(item) for item in clean_history):
            raise ValueError("Iroh room projected invalid timer history.")
        wall, counter = max(clocks)
        return {
            "canonicalTimer": timer,
            "history": clean_history,
            "tasks": tasks,
            "knownTasks": sorted(
                known_tasks.values(),
                key=lambda item: (item["title"].encode(), item["id"].encode()),
            ),
            "durationsMs": durations,
            "autoStartBreaks": auto_start,
            "selectedTaskId": selected_task_id,
            "hlcWallMs": wall,
            "hlcCounter": counter,
        }

    def project_iroh_expiry(self, now_ms: int | None = None) -> bool:
        room_id = self.active_iroh_room_id
        if self.replication_mode != "iroh" or room_id is None:
            return False
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._immediate_transaction():
            self._capture_local_iroh_records_locked(room_id)
            before = self.get_meta("snapshot", {}).get("canonicalTimer")
            projection = self._project_iroh_room(room_id, now_ms=now_ms)
            expired = (
                isinstance(before, dict)
                and before.get("status") == "running"
                and isinstance(projection.get("canonicalTimer"), dict)
                and projection["canonicalTimer"].get("id") == before.get("id")
                and projection["canonicalTimer"].get("status") == "completed"
            )
            timer = projection.get("canonicalTimer")
            settings = self._normalize_settings(self.get_meta("settings", {}))
            if (
                expired
                and isinstance(timer, dict)
                and settings["selectedPhase"] == timer.get("phase")
            ):
                settings["selectedPhase"] = (
                    next_break_phase(projection["history"], timer.get("anchorAt"))
                    if timer.get("phase") == "focus"
                    else "focus"
                )
                self._set_meta("settings", settings)
            workspace = self._workspace_with_iroh_projection(
                self._capture_workspace(), projection
            )
            self.connection.execute(
                "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                (json.dumps(workspace, separators=(",", ":")), room_id),
            )
            self._restore_workspace(workspace)
            timer = projection.get("canonicalTimer")
            if (
                expired
                and isinstance(timer, dict)
                and timer.get("phase") == "focus"
                and timer.get("startedByDeviceId") == self.device_id
                and projection["autoStartBreaks"]
            ):
                settings = self._normalize_settings(self.get_meta("settings", {}))
                phase = next_break_phase(projection["history"], timer.get("anchorAt"))
                trusted_ms, sequences, clocks = self._reserve_generation(
                    now_ms, sequence_count=1, clock_count=1, use_server_clock=False
                )
                self._queue_command(
                    "start",
                    None,
                    phase,
                    settings["durationsMs"],
                    None,
                    now_ms,
                    timer_now_ms=now_ms,
                    trusted_ms=trusted_ms,
                    sequence=sequences[0],
                    clock=clocks[0],
                    command_id=self._reserve_uuid7_ids(clocks[0][0], 1)[0],
                )
                self._capture_local_iroh_records_locked(room_id)
            return expired

    def insert_remote_iroh_records(
        self,
        room_id: str,
        records: list[dict[str, Any]],
        advertised_digests: dict[tuple[str, str], str] | None = None,
    ) -> bool:
        if not records:
            raise ValueError("Iroh operation batch must not be empty.")
        conflict: Exception | None = None
        with self._immediate_transaction():
            if advertised_digests is not None:
                from .iroh_protocol import record_digest, record_id

                returned = {
                    (record["domain"], record_id(record)): record_digest(record)
                    for record in records
                }
                if returned != advertised_digests:
                    raise ValueError(
                        "Fetched Iroh records do not match advertised inventory digests."
                    )
            active = self.active_iroh_room_id == room_id and self.replication_mode == "iroh"
            if active:
                self._capture_local_iroh_records_locked(room_id)
            try:
                inserted = self._insert_iroh_records_locked(room_id, records)
            except Exception as error:
                if error.__class__.__name__ != "ImmutableConflict":
                    raise
                inserted = False
                conflict = error
            if inserted:
                projection = self._project_iroh_room(room_id)
                room = self.connection.execute(
                    "SELECT workspace FROM iroh_rooms WHERE room_id = ?", (room_id,)
                ).fetchone()
                if room is None:
                    raise ValueError("Iroh room workspace is missing.")
                workspace = self._workspace_with_iroh_projection(
                    json.loads(room["workspace"]), projection
                )
                self.connection.execute(
                    "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                    (json.dumps(workspace, separators=(",", ":")), room_id),
                )
                if active:
                    self._restore_workspace(workspace)
        if conflict is not None:
            raise conflict
        return inserted

    def _insert_iroh_records_locked(
        self, room_id: str, records: list[dict[str, Any]]
    ) -> bool:
        from .iroh_protocol import (
            ImmutableConflict,
            MAX_OPERATION_REFS,
            record_digest,
            record_id,
            validate_record,
        )

        if len(records) > MAX_OPERATION_REFS:
            raise ValueError("Iroh operation batch exceeds 256 records.")
        room = self.connection.execute(
            "SELECT conflict FROM iroh_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if room is None:
            raise ValueError("Iroh room does not exist.")
        if room["conflict"] is not None:
            raise ImmutableConflict("Iroh room requires repair before replication can continue.")
        prepared = []
        keys = set()
        for record in records:
            validate_record(record)
            identifier = record_id(record)
            key = (record["domain"], identifier)
            if key in keys:
                raise ValueError("Iroh operation batch contains duplicate references.")
            keys.add(key)
            prepared.append((record, identifier, record_digest(record)))
        for record, identifier, digest in prepared:
            existing = self.connection.execute(
                "SELECT digest, record FROM iroh_records WHERE room_id = ? "
                "AND domain = ? AND operation_id = ?",
                (room_id, record["domain"], identifier),
            ).fetchone()
            if existing is not None and existing["digest"] != digest:
                evidence = {
                    "domain": record["domain"],
                    "id": identifier,
                    "localDigest": str(existing["digest"]),
                    "receivedDigest": digest,
                    "detectedAtMs": int(time.time() * 1000),
                }
                self.connection.execute(
                    "INSERT OR IGNORE INTO iroh_conflicts(room_id, domain, operation_id, "
                    "local_digest, received_digest, received_record, detected_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        room_id,
                        record["domain"],
                        identifier,
                        existing["digest"],
                        digest,
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                        evidence["detectedAtMs"],
                    ),
                )
                self.connection.execute(
                    "UPDATE iroh_rooms SET conflict = ? WHERE room_id = ?",
                    (json.dumps(evidence, separators=(",", ":")), room_id),
                )
                raise ImmutableConflict(
                    "Iroh room contains different immutable payloads for the same operation ID."
                )
        sequence_owners: dict[tuple[str, int], str] = {}
        for row in self.connection.execute(
            "SELECT device_id, operation_id, record FROM iroh_records "
            "WHERE room_id = ? AND domain = 'timer'",
            (room_id,),
        ):
            record = json.loads(row["record"])
            sequence_owners[(str(row["device_id"]), int(record["operation"]["deviceSequence"]))] = str(row["operation_id"])
        for record, identifier, _digest in prepared:
            if record["domain"] != "timer":
                continue
            key = (record["deviceId"], int(record["operation"]["deviceSequence"]))
            owner = sequence_owners.get(key)
            if owner is not None and owner != identifier:
                raise ValueError("Iroh timer operation reuses a device sequence.")
            sequence_owners[key] = identifier
        inserted = False
        for record, identifier, digest in prepared:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO iroh_records(room_id, domain, operation_id, "
                "device_id, digest, record) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    record["domain"],
                    identifier,
                    record["deviceId"],
                    digest,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            inserted = inserted or cursor.rowcount > 0
        return inserted

    def iroh_inventory(
        self, room_id: str, after: str | None, limit: int
    ) -> tuple[list[dict[str, str]], str | None]:
        from .iroh_protocol import MAX_INVENTORY

        if isinstance(limit, bool) or not 1 <= limit <= MAX_INVENTORY:
            raise ValueError("Iroh inventory limit must be 1 through 1024.")
        parameters: list[Any] = [room_id]
        where = "room_id = ?"
        if after is not None:
            if not isinstance(after, str) or after.count("\0") != 1:
                raise ValueError("Iroh inventory cursor is invalid.")
            domain, identifier = after.split("\0")
            where += " AND (domain > ? OR (domain = ? AND operation_id > ?))"
            parameters.extend((domain, domain, identifier))
        rows = self.connection.execute(
            "SELECT domain, operation_id, digest FROM iroh_records WHERE "
            + where
            + " ORDER BY domain, operation_id LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        entries = [
            {
                "domain": str(row["domain"]),
                "id": str(row["operation_id"]),
                "digest": str(row["digest"]),
            }
            for row in rows
        ]
        next_cursor = (
            f'{rows[-1]["domain"]}\0{rows[-1]["operation_id"]}'
            if has_more and rows
            else None
        )
        return entries, next_cursor

    def iroh_operations(
        self, room_id: str, references: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        from .iroh_protocol import MAX_OPERATION_REFS

        if not 1 <= len(references) <= MAX_OPERATION_REFS:
            raise ValueError("Iroh operation request must contain 1 through 256 references.")
        keys = [(item.get("domain"), item.get("id")) for item in references]
        if len(keys) != len(set(keys)):
            raise ValueError("Iroh operation request contains duplicate references.")
        records = []
        for domain, identifier in keys:
            row = self.connection.execute(
                "SELECT record FROM iroh_records WHERE room_id = ? AND domain = ? "
                "AND operation_id = ?",
                (room_id, domain, identifier),
            ).fetchone()
            if row is None:
                raise KeyError("Requested Iroh operation was not found.")
            records.append(json.loads(row["record"]))
        return records

    def missing_iroh_references(
        self, room_id: str, remote_entries: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        from .iroh_protocol import ImmutableConflict, MAX_INVENTORY

        if len(remote_entries) > MAX_INVENTORY:
            raise ValueError("Iroh inventory exceeds 1024 entries.")
        missing = []
        conflict: ImmutableConflict | None = None
        with self._immediate_transaction():
            for entry in remote_entries:
                row = self.connection.execute(
                    "SELECT digest FROM iroh_records WHERE room_id = ? AND domain = ? "
                    "AND operation_id = ?",
                    (room_id, entry["domain"], entry["id"]),
                ).fetchone()
                if row is None:
                    missing.append({"domain": entry["domain"], "id": entry["id"]})
                    continue
                if row["digest"] == entry["digest"]:
                    continue
                evidence = {
                    "domain": entry["domain"],
                    "id": entry["id"],
                    "localDigest": str(row["digest"]),
                    "receivedDigest": entry["digest"],
                    "detectedAtMs": int(time.time() * 1000),
                }
                self.connection.execute(
                    "INSERT OR IGNORE INTO iroh_conflicts(room_id, domain, operation_id, "
                    "local_digest, received_digest, received_record, detected_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (
                        room_id,
                        entry["domain"],
                        entry["id"],
                        row["digest"],
                        entry["digest"],
                        evidence["detectedAtMs"],
                    ),
                )
                self.connection.execute(
                    "UPDATE iroh_rooms SET conflict = ? WHERE room_id = ?",
                    (json.dumps(evidence, separators=(",", ":")), room_id),
                )
                conflict = ImmutableConflict(
                    "Iroh room inventory contains an immutable-ID conflict."
                )
                break
        if conflict is not None:
            raise conflict
        return missing

    def _upsert_iroh_peer(
        self,
        room_id: str,
        endpoint_id: str,
        endpoint_ticket: str,
        device_id: str | None,
        display_name: str | None,
        last_seen_at_ms: int | None,
    ) -> None:
        from .iroh_protocol import MAX_ENDPOINT_TICKET, MAX_PEERS

        if (
            not endpoint_id
            or not endpoint_ticket
            or len(endpoint_ticket.encode()) > MAX_ENDPOINT_TICKET
            or display_name is not None
            and not 1 <= len(display_name) <= 64
        ):
            raise ValueError("Iroh peer metadata is invalid.")
        exists = self.connection.execute(
            "SELECT 1 FROM iroh_peers WHERE room_id = ? AND endpoint_id = ?",
            (room_id, endpoint_id),
        ).fetchone()
        count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM iroh_peers WHERE room_id = ?", (room_id,)
        ).fetchone()["count"]
        if exists is None and count >= MAX_PEERS:
            raise ValueError("Iroh room address book contains 64 peers.")
        ticket_key = self._peer_ticket_key(room_id, endpoint_id)
        self._iroh_secret_store.save(ticket_key, endpoint_ticket.encode("utf-8"))
        self.connection.execute(
            "INSERT INTO iroh_peers(room_id, endpoint_id, endpoint_ticket, device_id, "
            "display_name, last_seen_at_ms) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(room_id, endpoint_id) DO UPDATE SET "
            "endpoint_ticket = excluded.endpoint_ticket, device_id = excluded.device_id, "
            "display_name = excluded.display_name, last_seen_at_ms = excluded.last_seen_at_ms",
            (
                room_id,
                endpoint_id,
                f"secure:{ticket_key}",
                device_id,
                display_name,
                last_seen_at_ms,
            ),
        )

    def upsert_iroh_peer(
        self,
        room_id: str,
        endpoint_id: str,
        endpoint_ticket: str,
        device_id: str | None,
        display_name: str | None,
        last_seen_at_ms: int | None = None,
    ) -> None:
        with self._immediate_transaction():
            self._upsert_iroh_peer(
                room_id,
                endpoint_id,
                endpoint_ticket,
                device_id,
                display_name,
                last_seen_at_ms,
            )

    def iroh_peers(self, room_id: str) -> list[dict[str, Any]]:
        peers = []
        for row in self.connection.execute(
            "SELECT endpoint_id, endpoint_ticket, device_id, display_name, "
            "last_seen_at_ms FROM iroh_peers WHERE room_id = ? "
            "ORDER BY last_seen_at_ms DESC, endpoint_id",
            (room_id,),
        ):
            endpoint_id = str(row["endpoint_id"])
            ticket = self._iroh_secret_store.load(
                self._peer_ticket_key(room_id, endpoint_id)
            )
            if ticket is None:
                raise ValueError("Saved Iroh peer capability is unavailable.")
            try:
                endpoint_ticket = ticket.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("Saved Iroh peer capability is invalid.") from error
            peers.append(
                {
                    "endpointId": endpoint_id,
                    "endpointTicket": endpoint_ticket,
                    "deviceId": row["device_id"],
                    "displayName": row["display_name"],
                    "lastSeenAtMs": row["last_seen_at_ms"],
                }
            )
        return peers

    def has_pending_auto_break(self) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM pending_auto_breaks LIMIT 1"
            ).fetchone()
            is not None
        )

    def provisional_auto_break_timer_ids(self) -> set[str]:
        timer_ids: set[str] = set()
        for row in self.connection.execute(
            "SELECT commands.payload FROM pending_auto_break_starts AS starts "
            "JOIN pending_commands AS commands ON commands.id = starts.start_command_id"
        ):
            timer_id = json.loads(row["payload"]).get("timerId")
            if isinstance(timer_id, str) and timer_id:
                timer_ids.add(timer_id)
        return timer_ids

    def load_with_provisional_auto_breaks(
        self,
    ) -> tuple[dict[str, Any], set[str]]:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            state = self.load(projection=True)
            timer_ids = self.provisional_auto_break_timer_ids()
        except BaseException:
            if owns_transaction:
                self.connection.rollback()
            raise
        if owns_transaction:
            self.connection.commit()
        return state, timer_ids

    def process_auto_break(
        self, *, require_canonical: bool, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            if self.pending_resolution() is not None:
                return []
            if self.has_pending_auto_break():
                self._reserve_generation(
                    now_ms,
                    clock_count=0,
                    use_server_clock=use_server_clock,
                    use_monotonic=use_server_clock,
                )
            while True:
                trigger = self.connection.execute(
                    "SELECT finish_command_id, timer_id, finish_device_sequence "
                    "FROM pending_auto_breaks ORDER BY rowid LIMIT 1"
                ).fetchone()
                if trigger is None:
                    return []
                finish_command_id = str(trigger["finish_command_id"])
                timer_id = str(trigger["timer_id"])
                finish_sequence = int(trigger["finish_device_sequence"])
                pending = self._physical_pending_commands([
                    json.loads(row["payload"])
                    for row in self.connection.execute(
                        "SELECT payload FROM pending_commands ORDER BY device_sequence"
                    )
                ])
                if any(
                    int(command.get("deviceSequence", 0)) > finish_sequence
                    and not (
                        command.get("type") == "finish"
                        and command.get("timerId") == timer_id
                    )
                    for command in pending
                ):
                    self.connection.execute(
                        "DELETE FROM pending_auto_breaks WHERE timer_id = ?",
                        (timer_id,),
                    )
                    continue
                source_finish_pending = any(
                    command.get("id") == finish_command_id for command in pending
                )
                if require_canonical and source_finish_pending:
                    return []

                snapshot = self.get_meta("snapshot", {})
                if require_canonical and self._pending_auto_start_operations():
                    return []
                canonical_timer = snapshot.get("canonicalTimer")
                base_history = snapshot.get("history", [])
                canonical_completion = next(
                    (
                        item
                        for item in base_history
                        if item.get("phase") == "focus"
                        and item.get("status") == "completed"
                        and item.get("timerId") == timer_id
                        and item.get("commandId") == finish_command_id
                    ),
                    None,
                )
                canonical_timer_is_source = canonical_timer is not None and (
                    canonical_timer.get("id") == timer_id
                    and canonical_timer.get("phase") == "focus"
                    and canonical_timer.get("status") == "completed"
                )
                source_already_accepted = (
                    not source_finish_pending
                    and canonical_completion is not None
                    and canonical_timer_is_source
                )
                optimistic_timer, optimistic_history = rebuild_optimistic(
                    canonical_timer, base_history, pending
                )
                current_timer = (
                    canonical_timer if require_canonical else optimistic_timer
                )
                if (
                    not source_already_accepted
                    and (
                        not isinstance(current_timer, dict)
                        or current_timer.get("id") != timer_id
                        or current_timer.get("phase") != "focus"
                        or current_timer.get("status") != "completed"
                    )
                ):
                    self.connection.execute(
                        "DELETE FROM pending_auto_breaks WHERE timer_id = ?",
                        (timer_id,),
                    )
                    continue
                settings = self._normalize_settings(self.get_meta("settings", {}))
                history = (
                    base_history
                    if require_canonical or source_already_accepted
                    else optimistic_history
                )
                completion = (
                    canonical_completion
                    if require_canonical or source_already_accepted
                    else next(
                        (
                            item
                            for item in history
                            if item.get("phase") == "focus"
                            and item.get("status") == "completed"
                            and item.get("timerId") == timer_id
                            and item.get("commandId") == finish_command_id
                        ),
                        None,
                    )
                )
                trusted_ms, sequences, clocks = self._reserve_generation(
                    now_ms,
                    sequence_count=1,
                    clock_count=1,
                    use_server_clock=use_server_clock,
                    use_monotonic=use_server_clock,
                )
                self.connection.execute(
                    "DELETE FROM pending_auto_breaks WHERE timer_id = ?",
                    (timer_id,),
                )
                if completion is None:
                    continue

                phase = next_break_phase(
                    history, completion.get("completedAt") or completion.get("endedAt")
                )
                settings["selectedPhase"] = phase
                self._set_meta("settings", settings)
                command_id = self._reserve_uuid7_ids(clocks[0][0], 1)[0]
                command = self._queue_command(
                    "start",
                    None,
                    phase,
                    settings["durationsMs"],
                    None,
                    now_ms,
                    (
                        finish_command_id
                        if not require_canonical and not source_already_accepted
                        else None
                    ),
                    trusted_ms=trusted_ms,
                    sequence=sequences[0],
                    clock=clocks[0],
                    command_id=command_id,
                )
                if not require_canonical and not source_already_accepted:
                    self.connection.execute(
                        "INSERT INTO pending_auto_break_starts("
                        "source_finish_command_id, source_timer_id, start_command_id, "
                        "selected_phase_version) VALUES (?, ?, ?, ?)",
                        (
                            finish_command_id,
                            timer_id,
                            command["id"],
                            int(self.get_meta("selectedPhaseVersion", 0)),
                        ),
                    )
                return [command]

    def reset_account_data(self) -> None:
        with self._immediate_transaction():
            room_id = self.active_iroh_room_id
            if self.replication_mode == "iroh" and room_id is not None:
                self._capture_local_iroh_records_locked(room_id)
                room = self.connection.execute(
                    "SELECT return_workspace FROM iroh_rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if room is None:
                    raise ValueError("Active Iroh room workspace is missing.")
                returned = json.loads(room["return_workspace"])
                return_snapshot = returned.get("metadata", {}).get("snapshot", {})
                preserve_return_domain = (
                    isinstance(return_snapshot, dict)
                    and return_snapshot.get("user") is None
                )
                cleared_current = self._workspace_without_account(
                    self._capture_workspace(), preserve_domain=True
                )
                cleared_return = self._workspace_without_account(
                    returned, preserve_domain=preserve_return_domain
                )
                self.connection.execute(
                    "UPDATE iroh_rooms SET return_workspace = ?, workspace = ? "
                    "WHERE room_id = ?",
                    (
                        json.dumps(cleared_return, separators=(",", ":")),
                        json.dumps(cleared_current, separators=(",", ":")),
                        room_id,
                    ),
                )
                self._restore_workspace(cleared_current)
                return
            self.connection.execute("DELETE FROM pending_commands")
            self.connection.execute("DELETE FROM pending_task_operations")
            self.connection.execute("DELETE FROM pending_duration_operations")
            self.connection.execute("DELETE FROM pending_auto_start_operations")
            self.connection.execute("DELETE FROM pending_selected_task_operations")
            self.connection.execute("DELETE FROM pending_auto_breaks")
            self.connection.execute("DELETE FROM pending_auto_break_starts")
            self.connection.execute("DELETE FROM pending_phase_advances")
            self._set_meta("commandPhysicalTimes", {})
            self._set_meta("centralizedTimerOwnership", None)
            self._set_meta("pendingSync", None)
            self._set_meta("pendingResolution", None)
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings["durations"] = {
                phase: int(definition["default_minutes"])
                for phase, definition in PHASES.items()
            }
            settings["durationsMs"] = {
                phase: int(definition["default_minutes"]) * 60_000
                for phase, definition in PHASES.items()
            }
            settings["selectedTaskId"] = None
            settings["autoStartBreaks"] = False
            self._set_meta("autoStartLegacyDefaultUnknown", False)
            self._set_meta("settings", settings)
            self._set_meta(
                "snapshot",
                {
                    "revision": 0,
                    "canonicalTimer": None,
                    "history": [],
                    "tasks": [],
                    "knownTasks": [],
                    "autoStartBreaks": False,
                    "selectedTaskId": None,
                    "user": None,
                },
            )
