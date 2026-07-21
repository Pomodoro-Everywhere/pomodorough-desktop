from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import PHASES, elapsed_ms


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
                "autoStartBreaks": False,
            },
            "snapshot": {"revision": 0, "canonicalTimer": None, "history": [], "user": None},
        }
        with self.connection:
            for key, value in defaults.items():
                self.connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                    (key, json.dumps(value, separators=(",", ":"))),
                )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.connection:
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
        return {"settings": settings, "snapshot": snapshot, "pending": pending}

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.set_meta("settings", settings)

    def queue_command(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations: dict[str, int],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        sequence = int(self.get_meta("deviceSequence", 0)) + 1
        old_hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
        wall_ms = max(now_ms, int(old_hlc.get("wallMs", 0)))
        counter = int(old_hlc.get("counter", 0)) + 1 if wall_ms == old_hlc.get("wallMs") else 0
        starting = command_type == "start"

        if starting:
            phase = selected_phase if selected_phase in PHASES else "focus"
            timer_id = str(uuid.uuid4())
            planned_ms = int(durations[phase]) * 60_000
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
        payload = json.dumps(command, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                "INSERT INTO pending_commands(id, device_sequence, payload) VALUES (?, ?, ?)",
                (command["id"], sequence, payload),
            )
            self._set_meta("deviceSequence", sequence)
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return command

    def sync_payload(self) -> dict[str, Any]:
        state = self.load()
        return {
            "deviceId": self.device_id,
            "lastRevision": int(state["snapshot"].get("revision", 0)),
            "commands": state["pending"][:256],
        }

    def apply_sync(self, response: dict[str, Any]) -> list[str]:
        notices: list[str] = []
        acknowledgements = response.get("acknowledgements", [])
        with self.connection:
            for acknowledgement in acknowledgements:
                self.connection.execute(
                    "DELETE FROM pending_commands WHERE id = ?",
                    (acknowledgement.get("commandId"),),
                )
                if acknowledgement.get("outcome") != "applied":
                    reason = acknowledgement.get("reason") or acknowledgement.get("outcome")
                    notices.append(str(reason))

            previous = self.get_meta("snapshot")
            self._set_meta(
                "snapshot",
                {
                    "revision": int(response["revision"]),
                    "canonicalTimer": response.get("canonicalTimer"),
                    "history": response.get("history", []),
                    "user": previous.get("user"),
                },
            )
            hlc = self.get_meta("hlc", {"wallMs": 0, "counter": 0})
            server_wall = int(response.get("serverHlcWallMs", 0))
            if server_wall > int(hlc.get("wallMs", 0)):
                self._set_meta("hlc", {"wallMs": server_wall, "counter": 0})
        return notices

    def set_user(self, user: dict[str, Any] | None) -> None:
        snapshot = self.get_meta("snapshot")
        snapshot["user"] = user
        self.set_meta("snapshot", snapshot)

    def reset_account_data(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM pending_commands")
            self._set_meta(
                "snapshot",
                {"revision": 0, "canonicalTimer": None, "history": [], "user": None},
            )
