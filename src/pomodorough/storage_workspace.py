from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any, Callable

from .core import PHASES

_METADATA_KEYS = (
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

_TABLE_COLUMNS = {
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


class WorkspacePersistence:
    def __init__(
        self,
        connection: sqlite3.Connection,
        read_meta: Callable[[str, Any], Any],
        write_meta: Callable[[str, Any], None],
        normalize_settings: Callable[[Any], dict[str, Any]],
    ) -> None:
        self._connection = connection
        self._read_meta = read_meta
        self._write_meta = write_meta
        self._normalize_settings = normalize_settings

    @staticmethod
    def table_columns() -> dict[str, tuple[str, ...]]:
        return dict(_TABLE_COLUMNS)

    def capture(self) -> dict[str, Any]:
        metadata = {key: self._read_meta(key, None) for key in _METADATA_KEYS}
        tables = {}
        for table, columns in _TABLE_COLUMNS.items():
            tables[table] = [
                {column: row[column] for column in columns}
                for row in self._connection.execute(
                    f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
                )
            ]
        return {"metadata": metadata, "tables": tables}

    def restore(self, workspace: dict[str, Any]) -> None:
        if (
            not isinstance(workspace, dict)
            or not isinstance(workspace.get("metadata"), dict)
            or not isinstance(workspace.get("tables"), dict)
        ):
            raise ValueError("Saved replication workspace is invalid.")
        for table in _TABLE_COLUMNS:
            self._connection.execute(f"DELETE FROM {table}")
        for table, columns in _TABLE_COLUMNS.items():
            self._restore_queue(table, columns, workspace["tables"].get(table, []))
        self.restore_metadata(workspace["metadata"])

    def _restore_queue(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: Any,
    ) -> None:
        if not isinstance(rows, list):
            raise ValueError("Saved replication queue is invalid.")
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError("Saved replication queue row is invalid.")
            self._connection.execute(
                f"INSERT INTO {table}({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )

    def restore_metadata(self, metadata: dict[str, Any]) -> None:
        for key, value in metadata.items():
            if key in _METADATA_KEYS:
                self._write_meta(key, value)

    def without_account(
        self,
        workspace: dict[str, Any],
        *,
        preserve_domain: bool,
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
        self._clear_domain(metadata, settings)
        for table in cleared["tables"]:
            cleared["tables"][table] = []
        return cleared

    @staticmethod
    def _clear_domain(metadata: dict[str, Any], settings: dict[str, Any]) -> None:
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

    @staticmethod
    def serialize(workspace: dict[str, Any]) -> str:
        return json.dumps(workspace, separators=(",", ":"))

    @staticmethod
    def deserialize(serialized: str) -> dict[str, Any]:
        workspace = json.loads(serialized)
        if not isinstance(workspace, dict):
            raise ValueError("Saved replication workspace is invalid.")
        return workspace

    def save_room(self, room_id: str, workspace: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
            (self.serialize(workspace), room_id),
        )
