from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.storage import Store
from pomodorough.storage_generation import GenerationReservation
from pomodorough.storage_completion import TimerCompletionPolicy
from pomodorough.storage_canonical import CanonicalResponseStorage
from pomodorough.storage_iroh_records import IrohRecordPersistence
from pomodorough.storage_replication import ReplicationStorage
from pomodorough.storage_sync import SyncStorage
from pomodorough.storage_workspace import WorkspacePersistence


class StorageArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self._temporary_directory.name) / "storage.sqlite3"
        self.store = Store(database_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    def test_store_composes_explicit_storage_responsibilities(self) -> None:
        self.assertEqual(Store.__bases__, (object,))
        self.assertIsInstance(self.store._workspace_storage, WorkspacePersistence)
        self.assertIsInstance(self.store._canonical_storage, CanonicalResponseStorage)
        self.assertIsInstance(self.store._iroh_record_storage, IrohRecordPersistence)
        self.assertIsInstance(self.store._sync_storage, SyncStorage)
        self.assertIsInstance(self.store._replication_storage, ReplicationStorage)
        self.assertIsInstance(self.store._generation_storage, GenerationReservation)
        self.assertIsInstance(self.store._completion_policy, TimerCompletionPolicy)
        self.assertIsNot(self.store._sync_storage._dependencies, self.store)
        self.assertIsNot(self.store._canonical_storage._dependencies, self.store)
        for component_name in (
            "_validation",
            "_acknowledgements",
            "_reconciliation",
            "_installation",
        ):
            component = getattr(self.store._canonical_storage, component_name)
            self.assertIs(component._dependencies, self.store._canonical_storage._dependencies)
        self.assertNotIn("_completion_plan", Store.__dict__)
        for responsibility in (
            WorkspacePersistence,
            CanonicalResponseStorage,
            IrohRecordPersistence,
            SyncStorage,
            ReplicationStorage,
        ):
            self.assertNotIn("__getattr__", responsibility.__dict__)
        self.assertIn("restore", WorkspacePersistence.__dict__)
        self.assertIn("apply_sync", CanonicalResponseStorage.__dict__)
        self.assertIn("inventory", IrohRecordPersistence.__dict__)
        self.assertNotIn("iroh_inventory", ReplicationStorage.__dict__)

    def test_generation_reservation_delegates_each_hlc_tick_to_shared_core(self) -> None:
        calls: list[tuple[str, object]] = []

        class ClockCore:
            @staticmethod
            def dispatch(operation: str, input_value: object) -> object:
                calls.append((operation, input_value))
                local = input_value["local"]  # type: ignore[index]
                now_ms = input_value["physicalNowMs"]  # type: ignore[index]
                wall_ms = max(local["wallMs"], now_ms)
                counter = local["counter"] + 1 if wall_ms == local["wallMs"] else 0
                return {"wallMs": wall_ms, "counter": counter}

        database_path = self.store.path
        self.store.close()
        self.store = Store(database_path, shared_core=ClockCore())
        reserved = self.store._reserve_generation(
            100, clock_count=2, use_server_clock=False
        )

        self.assertEqual(reserved, (100, [], [(100, 0), (100, 1)]))
        self.assertEqual([operation for operation, _input in calls], [
            "hlc.tick.v1", "hlc.tick.v1",
        ])

    def test_invalid_workspace_restore_rolls_back_all_changes(self) -> None:
        original_settings = self.store.load()["settings"]
        workspace = self.store._capture_workspace()
        workspace["metadata"]["settings"] = '{"focusMinutes":99}'
        workspace["tables"]["pending_commands"] = [{"unexpected": "column"}]

        with self.assertRaisesRegex(
            ValueError,
            "Saved replication queue row is invalid.",
        ):
            with self.store._immediate_transaction():
                self.store._restore_workspace(workspace)

        self.assertEqual(self.store.load()["settings"], original_settings)


if __name__ == "__main__":
    unittest.main()
