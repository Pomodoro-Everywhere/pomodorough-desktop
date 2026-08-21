from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import ImmutableConflict, room_id_for_secret
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

    def test_selected_task_is_captured_and_survives_restart_and_deselection(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))

        selected = self.store.set_selected_task_id("local-room-task", now_ms=1_000)

        loaded = self.store.load()
        self.assertEqual(loaded["settings"]["selectedTaskId"], "local-room-task")
        self.assertEqual(loaded["pendingSelectedTasks"], [])
        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], 2)
        record = self.store.iroh_operations(
            room_id, [{"domain": "selectedTask", "id": selected["id"]}]
        )[0]
        self.assertEqual(record["deviceId"], self.store.device_id)
        self.assertEqual(
            set(record["operation"]),
            {"id", "taskId", "occurredAt", "hlcWallMs", "hlcCounter"},
        )
        self.assertNotIn("deviceId", record["operation"])

        self.store.close()
        self.store = Store(self.path, iroh_secret_store=self.secrets)
        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], "local-room-task"
        )

        cleared = self.store.set_selected_task_id(None, now_ms=2_000)
        self.assertIsNone(
            self.store.iroh_operations(
                room_id, [{"domain": "selectedTask", "id": cleared["id"]}]
            )[0]["operation"]["taskId"]
        )
        self.assertIsNone(self.store.load()["settings"]["selectedTaskId"])

    def test_selected_task_projection_uses_deterministic_record_order(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        wall_ms = 1_786_000_000_000
        records = [
            self._selected_task_record("operation-early", "task-early", wall_ms - 1),
            self._selected_task_record(
                "operation-zulu", "task-operation-zulu", wall_ms, device_id="device-zulu"
            ),
            self._selected_task_record(
                "operation-alpha", "task-operation-a", wall_ms, device_id="device-zulu"
            ),
            self._selected_task_record(
                "operation-omega", "task-operation-z", wall_ms, device_id="device-zulu"
            ),
            self._selected_task_record(
                "operation-last", "task-device-loses", wall_ms, device_id="device-alpha"
            ),
        ]

        self.store.insert_remote_iroh_records(room_id, list(reversed(records)))

        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], "task-operation-zulu"
        )

    def test_selected_task_raw_identity_survives_task_deletion(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        task = task_from_title("Deleted but selected")
        wall_ms = 1_786_000_000_000
        upsert = {
            "domain": "task",
            "deviceId": "device-alpha",
            "operation": {
                "id": "operation-upsert",
                "taskId": task["id"],
                "type": "upsert",
                "title": task["title"],
                "occurredAt": utc_timestamp(wall_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": 0,
            },
        }
        selected = self._selected_task_record(
            "operation-select", task["id"], wall_ms, counter=1
        )
        deleted = {
            "domain": "task",
            "deviceId": "device-alpha",
            "operation": {
                "id": "operation-delete",
                "taskId": task["id"],
                "type": "delete",
                "occurredAt": utc_timestamp(wall_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": 2,
            },
        }

        self.store.insert_remote_iroh_records(room_id, [deleted, selected, upsert])

        state = self.store.load()
        self.assertEqual(state["snapshot"]["tasks"], [])
        self.assertIn(task, state["snapshot"]["knownTasks"])
        self.assertEqual(state["settings"]["selectedTaskId"], task["id"])
        self.assertEqual(state["snapshot"]["selectedTaskId"], task["id"])

    def test_joined_genesis_and_room_switch_restore_selected_tasks(self) -> None:
        central_task = task_from_title("Central task")
        self.store.queue_task_operation("upsert", central_task, now_ms=1_000)
        self.store.set_selected_task_id(central_task["id"], now_ms=2_000)
        central_before = self.store.load()
        secret = bytes(reversed(range(32)))
        room_id = room_id_for_secret(secret)
        self.store.prepare_iroh_join(
            room_id, secret, None, "endpoint-alpha", "ticket-alpha", now_ms=3_000
        )
        room_task = task_from_title("Room task")
        genesis = {
            "domain": "genesis",
            "deviceId": "device-remote",
            "operation": {
                "canonicalTimer": None,
                "history": [],
                "tasks": [room_task],
                "durationsMs": central_before["settings"]["durationsMs"],
                "autoStartBreaks": False,
                "selectedTaskId": room_task["id"],
                "hlcWallMs": 0,
                "hlcCounter": 0,
            },
        }
        self.store.insert_remote_iroh_records(room_id, [genesis])
        self.store.activate_joined_iroh_room(room_id)
        self.assertEqual(self.store.load()["settings"]["selectedTaskId"], room_task["id"])

        self.store.leave_iroh_room()
        self.assertEqual(self.store.load(), central_before)
        self.store.set_replication_mode("iroh")
        self.assertEqual(self.store.load()["settings"]["selectedTaskId"], room_task["id"])

    def test_account_clear_preserves_room_selected_task_and_scrubs_return_workspace(self) -> None:
        central_task = task_from_title("Account task")
        self.store.queue_task_operation("upsert", central_task, now_ms=1_000)
        self.store.set_selected_task_id(central_task["id"], now_ms=2_000)
        snapshot = self.store.get_meta("snapshot")
        snapshot["user"] = {"id": "user-account"}
        self.store.set_meta("snapshot", snapshot)
        room_id = self.store.create_iroh_room(bytes(range(32)))
        room_task = task_from_title("Room-only task")
        self.store.queue_task_operation("upsert", room_task, now_ms=3_000)
        self.store.set_selected_task_id(room_task["id"], now_ms=4_000)

        self.store.reset_account_data()

        self.assertEqual(self.store.load()["settings"]["selectedTaskId"], room_task["id"])
        self.assertIsNone(self.store.load()["snapshot"]["user"])
        self.assertGreater(self.store.iroh_room(room_id)["operationCount"], 1)
        self.store.leave_iroh_room()
        self.assertIsNone(self.store.load()["settings"]["selectedTaskId"])
        self.assertEqual(self.store.load()["snapshot"]["tasks"], [])

    @staticmethod
    def _selected_task_record(
        identifier: str,
        task_id: str | None,
        wall_ms: int,
        *,
        counter: int = 0,
        device_id: str = "device-alpha",
    ) -> dict[str, object]:
        return {
            "domain": "selectedTask",
            "deviceId": device_id,
            "operation": {
                "id": identifier,
                "taskId": task_id,
                "occurredAt": utc_timestamp(wall_ms),
                "hlcWallMs": wall_ms,
                "hlcCounter": counter,
            },
        }

    def test_iroh_expiry_advances_every_phase_with_or_without_auto_start(self) -> None:
        start_ms = 1_790_000_000_000
        expected = {
            "focus": "short_break",
            "short_break": "focus",
            "long_break": "focus",
        }
        for auto_start in (False, True):
            for phase, next_phase in expected.items():
                with self.subTest(auto_start=auto_start, phase=phase):
                    path = Path(self.temporary.name) / f"{auto_start}-{phase}.sqlite3"
                    store = Store(path, iroh_secret_store=MemorySecretStore())
                    try:
                        store.create_iroh_room(bytes(range(32)))
                        if auto_start:
                            store.set_auto_start_breaks(True, now_ms=start_ms - 1)
                        duration_ms = store.load()["settings"]["durationsMs"][phase]
                        store.set_selected_phase(phase)
                        store.queue_command(
                            "start",
                            None,
                            phase,
                            store.load()["settings"]["durationsMs"],
                            now_ms=start_ms,
                        )

                        self.assertTrue(
                            store.project_iroh_expiry(start_ms + duration_ms)
                        )

                        self.assertEqual(
                            store.load()["settings"]["selectedPhase"], next_phase
                        )
                    finally:
                        store.close()

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
