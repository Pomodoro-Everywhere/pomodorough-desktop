from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.storage import Store
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
