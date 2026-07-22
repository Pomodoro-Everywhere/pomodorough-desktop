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

from .core import (
    PHASES,
    elapsed_ms,
    parse_timestamp_ms,
    rebuild_optimistic,
    task_from_title,
)

DURATION_MIN_MS = 60_000
PREFERENCE_DURATION_MAX_MS = 10_800_000
CANONICAL_DURATION_MAX_MS = 14_400_000
RESOLUTION_OPERATION_MAX = 4_096
ACKNOWLEDGEMENT_OUTCOMES = {"applied", "ignored", "rejected"}


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
            "pendingResolution": None,
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
            "pendingResolution": self.get_meta("pendingResolution"),
        }

    def save_settings(self, settings: dict[str, Any]) -> None:
        candidate = self._normalize_settings(settings)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            current = self._normalize_settings(self.get_meta("settings", {}))
            candidate["durations"] = current["durations"]
            candidate["durationsMs"] = current["durationsMs"]
            self._set_meta("settings", candidate)

    def _set_local_setting(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
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
            self._ensure_no_pending_resolution()
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
            self._ensure_no_pending_resolution()
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
            self._ensure_no_pending_resolution()
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
        self._ensure_no_pending_resolution()
        state = self.load()
        return {
            "deviceId": self.device_id,
            "lastRevision": int(state["snapshot"].get("revision", 0)),
            "commands": state["pending"][:256],
            "taskOperations": state["pendingTasks"][:256],
            "durationOperations": state["pendingDurations"][:256],
        }

    def pending_resolution(self, user_id: str | None = None) -> dict[str, Any] | None:
        pending = self.get_meta("pendingResolution")
        if not isinstance(pending, dict):
            return None
        owner = pending.get("owner")
        request = pending.get("request")
        queue_ids = pending.get("queueIds")
        if (
            not isinstance(owner, dict)
            or not isinstance(request, dict)
            or not isinstance(queue_ids, dict)
            or set(queue_ids) != {"commands", "taskOperations", "durationOperations"}
            or any(
                not isinstance(ids, list)
                or any(not isinstance(item_id, str) for item_id in ids)
                or len(ids) != len(set(ids))
                for ids in queue_ids.values()
            )
        ):
            return None
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
        self, response: dict[str, Any]
    ) -> dict[str, Any]:
        state = self.load()
        canonical = self._validated_sync_response(
            response,
            {"commands": [], "taskOperations": [], "durationOperations": []},
        )
        _timer, local_history = rebuild_optimistic(
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
            or state["snapshot"].get("canonicalTimer")
            or state["snapshot"].get("history")
            or state["snapshot"].get("tasks")
        )
        if local_history_exists and remote_history_exists:
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
            or expected_revision < 0
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
            state = self.load()
            operations = state if strategy != "keep_remote" else None
            outbound = {
                "commands": operations["pending"] if operations else [],
                "taskOperations": operations["pendingTasks"] if operations else [],
                "durationOperations": (
                    operations["pendingDurations"] if operations else []
                ),
            }
            labels = {
                "commands": "timer commands",
                "taskOperations": "task operations",
                "durationOperations": "duration operations",
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
                "commands": [item["id"] for item in state["pending"]],
                "taskOperations": [item["id"] for item in state["pendingTasks"]],
                "durationOperations": [
                    item["id"] for item in state["pendingDurations"]
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
        for acknowledgement in response_items:
            if (
                not isinstance(acknowledgement, dict)
                or not isinstance(acknowledgement.get(acknowledgement_id_key), str)
                or acknowledgement.get("outcome") not in ACKNOWLEDGEMENT_OUTCOMES
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
            "revision",
            "canonicalTimer",
            "history",
            "tasks",
            "durationsMs",
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
        canonical_durations = self._canonical_durations(response["durationsMs"])
        revision = response["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
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
        canonical_timer = response["canonicalTimer"]
        if canonical_timer is not None and not self._valid_canonical_timer(
            canonical_timer
        ):
            raise ValueError("Server returned an invalid canonical timer.")
        server_hlc_wall_ms = response["serverHlcWallMs"]
        server_hlc_counter = response["serverHlcCounter"]
        if (
            isinstance(server_hlc_wall_ms, bool)
            or not isinstance(server_hlc_wall_ms, int)
            or server_hlc_wall_ms < 0
            or isinstance(server_hlc_counter, bool)
            or not isinstance(server_hlc_counter, int)
            or server_hlc_counter < 0
        ):
            raise ValueError("Server returned an invalid logical clock.")
        return {
            "acknowledgements": acknowledgements,
            "taskAcknowledgements": task_acknowledgements,
            "durationAcknowledgements": duration_acknowledgements,
            "revision": revision,
            "canonicalTimer": canonical_timer,
            "history": history,
            "tasks": tasks,
            "durationsMs": canonical_durations,
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
        )
        for key, statement in groups:
            self.connection.executemany(
                statement, ((item_id,) for item_id in queue_ids[key])
            )

    def _install_canonical(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        *,
        preserve_known_tasks: bool,
    ) -> None:
        settings = self._normalize_settings(self.get_meta("settings", {}))
        settings["durationsMs"] = canonical["durationsMs"]
        settings["durations"] = {
            phase: self._display_minutes(duration_ms)
            for phase, duration_ms in canonical["durationsMs"].items()
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
                "user": user,
            },
        )
        hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
        merged_wall, merged_counter = max(
            (int(time.time() * 1000), 0),
            (int(hlc.get("wallMs", 0)), int(hlc.get("counter", 0))),
            (canonical["serverHlcWallMs"], canonical["serverHlcCounter"]),
        )
        self._set_meta("hlc", {"wallMs": merged_wall, "counter": merged_counter})

    def apply_sync(
        self, response: dict[str, Any], request: dict[str, Any]
    ) -> list[str]:
        with self._immediate_transaction():
            previous = self.get_meta("snapshot")
            canonical = self._validated_sync_response(response, request)
            if canonical["revision"] < int(previous.get("revision", 0)):
                raise ValueError("Server response would regress canonical revision.")
            notices = self._apply_acknowledgements(canonical)
            self._install_canonical(
                canonical,
                previous.get("user"),
                preserve_known_tasks=True,
            )
        return notices

    def apply_resolution(
        self,
        response: dict[str, Any],
        user: dict[str, Any],
        request_id: str | None = None,
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
                for key in ("commands", "taskOperations", "durationOperations")
            }
            if strategy == "keep_remote":
                if any(request_ids.values()):
                    raise ValueError("Keep-remote resolution contains local operations.")
            elif request_ids != queue_ids:
                raise ValueError(
                    "Pending history resolution does not match captured queue IDs."
                )
            notices = self._apply_acknowledgements(canonical, delete=False)
            self._delete_resolution_queue_ids(queue_ids)
            self._install_canonical(
                canonical,
                user,
                preserve_known_tasks=strategy != "keep_remote",
            )
            self._set_meta("pendingResolution", None)
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
