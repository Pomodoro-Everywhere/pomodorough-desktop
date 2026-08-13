from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pomodorough.iroh_protocol import ImmutableConflict
from pomodorough.storage import Store, utc_timestamp


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class IrohStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.secrets = MemorySecretStore()
        self.store = Store(self.path, iroh_secret_store=self.secrets)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_migration_is_transactional_and_idempotent(self) -> None:
        self.assertEqual(self.store.get_meta("irohSchemaVersion"), 1)
        expected = {"iroh_rooms", "iroh_records", "iroh_peers", "iroh_conflicts"}
        tables = {
            str(row["name"])
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(expected <= tables)

        self.store.close()
        self.store = Store(self.path, iroh_secret_store=self.secrets)
        self.assertEqual(self.store.get_meta("irohSchemaVersion"), 1)
        self.assertEqual(self.store.replication_mode, "centralized")

    def test_room_writes_are_durable_and_mode_switching_is_non_destructive(self) -> None:
        local_before = self.store.load()
        room_id = self.store.create_iroh_room(bytes(range(32)), "Design desk")
        command = self.store.queue_command(
            "start",
            None,
            "focus",
            self.store.load()["settings"]["durationsMs"],
            now_ms=1_786_000_000_000,
        )

        self.assertEqual(self.store.replication_mode, "iroh")
        self.assertEqual(self.store.load()["pending"], [])
        self.assertEqual(
            self.store.iroh_operations(
                room_id, [{"domain": "timer", "id": command["id"]}]
            )[0]["operation"],
            command,
        )

        self.store.leave_iroh_room()
        self.assertEqual(self.store.replication_mode, "offline")
        self.assertEqual(self.store.load(), local_before)
        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 2)

        self.store.set_replication_mode("centralized")
        self.store.set_replication_mode("iroh")
        self.assertEqual(
            self.store.load()["snapshot"]["canonicalTimer"]["id"],
            command["timerId"],
        )

        self.store.close()
        self.store = Store(self.path, iroh_secret_store=self.secrets)
        self.assertEqual(self.store.replication_mode, "iroh")
        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 2)

    def test_selected_task_remains_local_in_iroh_mode(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))

        self.assertIsNone(
            self.store.set_selected_task_id("local-room-task", now_ms=1_000)
        )

        loaded = self.store.load()
        self.assertEqual(loaded["settings"]["selectedTaskId"], "local-room-task")
        self.assertEqual(loaded["pendingSelectedTasks"], [])
        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 1)
        inventory, cursor = self.store.iroh_inventory(room_id, None, 1_024)
        self.assertIsNone(cursor)
        self.assertNotIn(
            "selectedTask",
            {item["domain"] for item in inventory},
        )

    def test_remote_batch_is_atomic_idempotent_and_conflicts_stop_room(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        first = self._duration_record("operation-1", 1_800_000)
        second = self._duration_record("operation-2", 900_000, phase="long_break")

        self.assertTrue(self.store.insert_remote_iroh_records(room_id, [first, second]))
        self.assertFalse(self.store.insert_remote_iroh_records(room_id, [first, second]))
        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 3)

        conflicting = self._duration_record("operation-1", 2_700_000)
        with self.assertRaises(ImmutableConflict):
            self.store.insert_remote_iroh_records(room_id, [conflicting])

        room = self.store.iroh_room(room_id)
        self.assertEqual(room["conflict"]["id"], "operation-1")
        self.assertNotEqual(
            room["conflict"]["localDigest"], room["conflict"]["receivedDigest"]
        )
        evidence = self.store.connection.execute(
            "SELECT received_record FROM iroh_conflicts WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        self.assertEqual(json.loads(evidence["received_record"]), conflicting)
        with self.assertRaises(ImmutableConflict):
            self.store.insert_remote_iroh_records(room_id, [second])

    def test_invalid_remote_batch_rolls_back_all_records(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        valid = self._duration_record("operation-valid", 1_800_000)
        invalid = self._duration_record("operation-invalid", 1_800_001)

        with self.assertRaises(ValueError):
            self.store.insert_remote_iroh_records(room_id, [valid, invalid])

        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 1)

    def test_auto_start_wire_record_uses_wrapper_origin_only(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        operation = self.store.set_auto_start_breaks(True, now_ms=1_786_000_000_000)

        record = self.store.iroh_operations(
            room_id, [{"domain": "autoStart", "id": operation["id"]}]
        )[0]

        self.assertEqual(record["deviceId"], self.store.device_id)
        self.assertNotIn("deviceId", record["operation"])
        self.assertTrue(self.store.load()["settings"]["autoStartBreaks"])

    @staticmethod
    def _duration_record(
        identifier: str, duration_ms: int, *, phase: str = "focus"
    ) -> dict[str, object]:
        wall_ms = 1_786_000_000_000
        return {
            "domain": "duration",
            "deviceId": "device-alpha",
            "operation": {
                "id": identifier,
                "phase": phase,
                "durationMs": duration_ms,
                "occurredAt": utc_timestamp(wall_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": 0,
            },
        }


if __name__ == "__main__":
    unittest.main()
