from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import PHASES, elapsed_ms, task_from_title

DURATION_MIN_MS = 60_000
DURATION_MAX_MS = 10_800_000


def default_data_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "pomodorough" / "pomodorough.sqlite3"


def utc_timestamp(milliseconds: int) -> str:
    value = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_path()
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
                payload TEXT NOT NULL
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
            """
        )
        self.connection.commit()
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        defaults: dict[str, Any] = {
            "deviceId": f"desktop-{uuid.uuid4()}",
            "deviceSequence": 0,
            "hlc": {"wallMs": 0, "counter": 0},
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
                "user": None,
            },
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
            changed = False
            for key, value in (("tasks", []), ("knownTasks", [])):
                if key not in snapshot:
                    snapshot[key] = value
                    changed = True
            if changed:
                self._set_meta("snapshot", snapshot)

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
                not DURATION_MIN_MS <= duration_ms <= DURATION_MAX_MS
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
    def _duration_ms(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Duration must be an integer number of milliseconds.")
        if not DURATION_MIN_MS <= value <= DURATION_MAX_MS:
            raise ValueError("Duration must be between 60000 and 10800000 milliseconds.")
        if value % 60_000:
            raise ValueError("Duration must be measured in whole minutes.")
        return value

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

    def load(self) -> dict[str, Any]:
        settings = self.get_meta("settings")
        snapshot = self.get_meta("snapshot")
        pending = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_commands ORDER BY device_sequence"
            )
        ]
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
        return {
            "settings": settings,
            "snapshot": snapshot,
            "pending": pending,
            "pendingTasks": pending_tasks,
            "pendingDurations": pending_durations,
        }

    def save_settings(self, settings: dict[str, Any]) -> None:
        candidate = self._normalize_settings(settings)
        with self._immediate_transaction():
            current = self._normalize_settings(self.get_meta("settings", {}))
            candidate["durations"] = current["durations"]
            candidate["durationsMs"] = current["durationsMs"]
            self._set_meta("settings", candidate)

    def _set_local_setting(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings[key] = value
            self._set_meta("settings", settings)

    def set_selected_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        self._set_local_setting("selectedPhase", phase)

    def set_auto_start_breaks(self, enabled: bool) -> None:
        self._set_local_setting("autoStartBreaks", bool(enabled))

    def set_selected_task_id(self, task_id: str | None) -> None:
        self._set_local_setting("selectedTaskId", task_id)

    def queue_command(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            sequence = int(self.get_meta("deviceSequence", 0)) + 1
            old_hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
            old_wall_ms = int(old_hlc.get("wallMs", 0))
            wall_ms = max(now_ms, old_wall_ms)
            counter = int(old_hlc.get("counter", 0)) + 1 if wall_ms == old_wall_ms else 0
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
                observed_ms = round(elapsed_ms(timer, now_ms))

            command = {
                "id": str(uuid.uuid4()),
                "deviceSequence": sequence,
                "timerId": timer_id,
                "type": command_type,
                "phase": phase,
                "plannedDurationMs": planned_ms,
                "occurredAt": utc_timestamp(now_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": counter,
                "observedElapsedMs": observed_ms,
            }
            if starting and phase == "focus" and selected_task_id:
                command["taskId"] = selected_task_id
            self.connection.execute(
                "INSERT INTO pending_commands(id, device_sequence, payload) VALUES (?, ?, ?)",
                (
                    command["id"],
                    sequence,
                    json.dumps(command, separators=(",", ":")),
                ),
            )
            self._set_meta("deviceSequence", sequence)
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
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

        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            old_hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
            old_wall_ms = int(old_hlc.get("wallMs", 0))
            wall_ms = max(now_ms, old_wall_ms)
            counter = int(old_hlc.get("counter", 0)) + 1 if wall_ms == old_wall_ms else 0
            operation: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "taskId": normalized["id"],
                "type": operation_type,
                "occurredAt": utc_timestamp(now_ms),
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
                known.values(), key=lambda item: (item["title"].casefold(), item["id"])
            )
            self._set_meta("snapshot", snapshot)
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    def queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_duration_operation(
                phase, duration_ms, settings, now_ms
            )
        return operation

    def _queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
    ) -> dict[str, Any]:
        duration_ms = self._duration_ms(duration_ms)
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
        else:
            occurred_ms = max(1, now_ms)
            old_hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
            old_wall_ms = int(old_hlc.get("wallMs", 0))
            wall_ms = max(occurred_ms, old_wall_ms)
            counter = int(old_hlc.get("counter", 0)) + 1 if wall_ms == old_wall_ms else 0
            occurred_at = utc_timestamp(occurred_ms)
        operation = {
            "id": str(uuid.uuid4()),
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

    def sync_payload(self) -> dict[str, Any]:
        state = self.load()
        return {
            "deviceId": self.device_id,
            "lastRevision": int(state["snapshot"].get("revision", 0)),
            "commands": state["pending"][:256],
            "taskOperations": state["pendingTasks"][:256],
            "durationOperations": state["pendingDurations"][:256],
        }

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
        for acknowledgement in response_items:
            if (
                not isinstance(acknowledgement, dict)
                or not isinstance(acknowledgement.get(acknowledgement_id_key), str)
                or not isinstance(acknowledgement.get("outcome"), str)
                or not isinstance(acknowledgement.get("reason"), str)
            ):
                raise ValueError(f"Sync returned invalid {label} acknowledgements.")
            acknowledged_ids.append(acknowledgement[acknowledgement_id_key])
        if (
            any(not isinstance(item_id, str) for item_id in sent_ids)
            or len(sent_ids) != len(set(sent_ids))
            or len(acknowledged_ids) != len(set(acknowledged_ids))
            or set(acknowledged_ids) != set(sent_ids)
        ):
            raise ValueError(
                f"Sync returned an invalid {label} acknowledgement set."
            )
        return response_items

    def apply_sync(
        self, response: dict[str, Any], request: dict[str, Any]
    ) -> list[str]:
        notices: list[str] = []
        with self._immediate_transaction():
            acknowledgements = self._validate_acknowledgements(
                request.get("commands"),
                response.get("acknowledgements"),
                "commandId",
                "command",
            )
            task_acknowledgements = self._validate_acknowledgements(
                request.get("taskOperations"),
                response.get("taskAcknowledgements"),
                "operationId",
                "task",
            )
            duration_acknowledgements = self._validate_acknowledgements(
                request.get("durationOperations"),
                response.get("durationAcknowledgements"),
                "operationId",
                "duration",
            )
            canonical_durations = self._canonical_durations(
                response.get("durationsMs")
            )

            for acknowledgement in acknowledgements:
                self.connection.execute(
                    "DELETE FROM pending_commands WHERE id = ?",
                    (acknowledgement["commandId"],),
                )
                if acknowledgement["outcome"] != "applied":
                    notices.append(
                        acknowledgement["reason"] or acknowledgement["outcome"]
                    )

            for acknowledgement in task_acknowledgements:
                self.connection.execute(
                    "DELETE FROM pending_task_operations WHERE id = ?",
                    (acknowledgement["operationId"],),
                )
                if acknowledgement["outcome"] != "applied":
                    notices.append(
                        acknowledgement["reason"] or acknowledgement["outcome"]
                    )

            for acknowledgement in duration_acknowledgements:
                self.connection.execute(
                    "DELETE FROM pending_duration_operations WHERE id = ?",
                    (acknowledgement["operationId"],),
                )
                if acknowledgement["outcome"] != "applied":
                    reason = acknowledgement["reason"] or acknowledgement["outcome"]
                    notices.append(reason)

            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings["durationsMs"] = canonical_durations
            settings["durations"] = {
                phase: self._display_minutes(duration_ms)
                for phase, duration_ms in canonical_durations.items()
            }
            for row in self.connection.execute(
                "SELECT payload FROM pending_duration_operations ORDER BY rowid"
            ):
                operation = json.loads(row["payload"])
                phase = operation.get("phase")
                if phase in PHASES:
                    duration_ms = self._duration_ms(operation.get("durationMs"))
                    settings["durationsMs"][phase] = duration_ms
                    settings["durations"][phase] = self._display_minutes(duration_ms)
            self._set_meta("settings", settings)

            previous = self.get_meta("snapshot")
            tasks = response.get("tasks", previous.get("tasks", []))
            known = {
                task["id"]: task
                for task in previous.get("knownTasks", [])
                if task.get("id") and task.get("title")
            }
            for task in tasks:
                if task.get("id") and task.get("title"):
                    known[task["id"]] = task
            self._set_meta(
                "snapshot",
                {
                    "revision": int(response["revision"]),
                    "canonicalTimer": response.get("canonicalTimer"),
                    "history": response.get("history", []),
                    "tasks": tasks,
                    "knownTasks": sorted(
                        known.values(),
                        key=lambda item: (item["title"].casefold(), item["id"]),
                    ),
                    "user": previous.get("user"),
                },
            )
            hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
            merged_wall, merged_counter = max(
                (int(time.time() * 1000), 0),
                (int(hlc.get("wallMs", 0)), int(hlc.get("counter", 0))),
                (int(response["serverHlcWallMs"]), int(response["serverHlcCounter"])),
            )
            self._set_meta(
                "hlc", {"wallMs": merged_wall, "counter": merged_counter}
            )
        return notices

    def set_user(self, user: dict[str, Any] | None) -> None:
        with self._immediate_transaction():
            snapshot = self.get_meta("snapshot")
            snapshot["user"] = user
            self._set_meta("snapshot", snapshot)

    def reset_account_data(self) -> None:
        with self._immediate_transaction():
            self.connection.execute("DELETE FROM pending_commands")
            self.connection.execute("DELETE FROM pending_task_operations")
            self.connection.execute("DELETE FROM pending_duration_operations")
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
            self._set_meta("settings", settings)
            self._set_meta(
                "snapshot",
                {
                    "revision": 0,
                    "canonicalTimer": None,
                    "history": [],
                    "tasks": [],
                    "knownTasks": [],
                    "user": None,
                },
            )
