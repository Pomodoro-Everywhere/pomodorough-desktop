from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from pomodorough.storage import Store, utc_timestamp
from pomodorough.storage_replication import ReplicationStorage
from pomodorough.storage_replication_coordination import (
    ReplicationTransactionCoordinator,
)
from pomodorough.storage_replication_lifecycle import RoomWorkspaceLifecycle
from pomodorough.storage_replication_projection import (
    GeneratedBreakPlanner,
    ReplicatedStateProjection,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_saves = False

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if self.fail_saves:
            raise OSError("secret store unavailable")
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class ReplicationStorageModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.secrets = MemorySecretStore()
        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=self.secrets,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_facade_composes_deep_modules_without_forwarding_methods(self) -> None:
        facade = self.store._replication_storage
        self.assertIsInstance(facade, ReplicationStorage)
        self.assertIsInstance(facade._rooms, RoomWorkspaceLifecycle)
        self.assertIsInstance(facade._projection, ReplicatedStateProjection)
        self.assertIsInstance(facade._break_planner, GeneratedBreakPlanner)
        self.assertIsInstance(
            facade._transactions,
            ReplicationTransactionCoordinator,
        )
        projection_dependencies = facade._projection._dependencies
        transaction_dependencies = facade._transactions._dependencies
        room_dependencies = facade._rooms._dependencies
        self.assertIs(projection_dependencies.workspace, self.store._workspace_storage)
        self.assertIs(transaction_dependencies.workspace, self.store._workspace_storage)
        self.assertIs(room_dependencies.workspace, self.store._workspace_storage)
        self.assertIs(transaction_dependencies.records, self.store._iroh_record_storage)
        self.assertIs(transaction_dependencies.projection, facade._projection)
        self.assertIs(transaction_dependencies.break_planner, facade._break_planner)
        self.assertIs(room_dependencies.projection, facade._projection)
        self.assertIs(room_dependencies.transactions, facade._transactions)
        self.assertIs(room_dependencies.device_id, transaction_dependencies.device_id)

        for component in (
            facade,
            facade._rooms,
            facade._projection,
            facade._transactions,
        ):
            self.assertEqual(type(component).__bases__, (object,))
            self.assertNotIn("_store", component.__dict__)
            self.assertNotIn("__getattr__", type(component).__dict__)

        forwarded = {
            name
            for name, member in ReplicationStorage.__dict__.items()
            if inspect.isfunction(member) and name != "__init__"
        }
        self.assertEqual(forwarded, set())
        self.assertIs(facade.create_iroh_room.__self__, facade._rooms)
        self.assertIs(
            facade.insert_remote_iroh_records.__self__,
            facade._transactions,
        )
        self.assertIs(
            facade._projected_local_genesis.__self__,
            facade._projection,
        )

    def test_room_switch_preserves_exact_workspace_bytes(self) -> None:
        serialize = self.store._workspace_storage.serialize
        capture = self.store._workspace_storage.capture
        local_bytes = serialize(capture())

        room_id = self.store.create_iroh_room(
            bytes(range(32)),
            "Byte stable",
            now_ms=1_786_000_000_000,
        )
        row = self.store.connection.execute(
            "SELECT return_workspace, workspace FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        self.assertEqual(row["return_workspace"], local_bytes)
        room_bytes = str(row["workspace"])
        self.assertEqual(room_bytes, serialize(capture()))

        self.store.leave_iroh_room()
        self.assertEqual(serialize(capture()), local_bytes)
        self.store.set_replication_mode("iroh")
        self.assertEqual(serialize(capture()), room_bytes)

    def test_room_creation_rolls_back_workspace_when_secret_save_fails(self) -> None:
        serialize = self.store._workspace_storage.serialize
        capture = self.store._workspace_storage.capture
        before = serialize(capture())
        self.secrets.fail_saves = True

        with self.assertRaisesRegex(OSError, "secret store unavailable"):
            self.store.create_iroh_room(
                bytes(range(32)),
                now_ms=1_786_000_000_000,
            )

        self.assertEqual(serialize(capture()), before)
        self.assertEqual(self.store.replication_mode, "centralized")
        self.assertIsNone(self.store.active_iroh_room_id)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) AS count FROM iroh_rooms"
            ).fetchone()["count"],
            0,
        )

    def test_remote_timer_owner_cannot_generate_local_auto_break(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        started_at_ms = 1_790_000_000_000
        self.store.set_auto_start_breaks(True, now_ms=started_at_ms - 1)
        duration_ms = self.store.load()["settings"]["durationsMs"]["focus"]
        remote_start = {
            "domain": "timer",
            "deviceId": "device-remote",
            "operation": {
                "id": "command-remote-start",
                "deviceSequence": 1,
                "timerId": "timer-remote",
                "type": "start",
                "phase": "focus",
                "plannedDurationMs": duration_ms,
                "occurredAt": utc_timestamp(started_at_ms),
                "hlcWallMs": started_at_ms,
                "hlcCounter": 0,
                "observedElapsedMs": 0,
            },
        }
        self.store.insert_remote_iroh_records(room_id, [remote_start])
        before = self.store.iroh_room(room_id)["operationCount"]

        self.assertTrue(self.store.project_iroh_expiry(started_at_ms + duration_ms))

        self.assertEqual(self.store.iroh_room(room_id)["operationCount"], before)
        self.assertEqual(self.store.load()["pending"], [])


if __name__ == "__main__":
    unittest.main()
