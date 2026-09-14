from __future__ import annotations

import inspect
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from pomodorough.core import task_from_title
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
        self.fail_save_key: str | None = None
        self.fail_delete_key: str | None = None

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if self.fail_saves or key == self.fail_save_key:
            raise OSError("secret store unavailable")
        self.values[key] = value

    def delete(self, key: str) -> None:
        if key == self.fail_delete_key:
            self.fail_delete_key = None
            raise OSError("secret deletion unavailable")
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

    def test_delivery_proof_follows_workspace_across_switch_and_reopen(
        self,
    ) -> None:
        self.store.queue_task_operation(
            "upsert", task_from_title("Central proof task"), now_ms=1_000
        )
        central = self.store.delivery_proof()
        self.assertTrue(central["taskOperations"])
        self.store.set_meta("canonicalHead", {"wallMs": 7, "counter": 1})
        self.store.create_iroh_room(bytes(range(32)))
        self.assertEqual(
            self.store.delivery_proof(),
            {domain: [] for domain in central},
        )
        self.assertIsNone(self.store.canonical_head())
        self.store.queue_task_operation(
            "upsert", task_from_title("Room proof task"), now_ms=2_000
        )
        room = self.store.delivery_proof()
        # Iroh mode publishes mutations to room records immediately, so
        # the durable proof is empty while the record holds the payload.
        self.assertEqual(room, {domain: [] for domain in central})
        self.assertNotEqual(room, central)
        self.store.set_meta("canonicalHead", {"wallMs": 9, "counter": 0})
        self.store.leave_iroh_room()
        self.assertEqual(self.store.delivery_proof(), central)
        self.assertEqual(self.store.canonical_head(), (7, 1))
        self.store.close()
        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3", iroh_secret_store=self.secrets
        )
        self.store.set_replication_mode("iroh")
        self.assertEqual(self.store.delivery_proof(), room)
        self.assertEqual(self.store.canonical_head(), (9, 0))
        self.store.leave_iroh_room()
        self.assertEqual(self.store.delivery_proof(), central)

    def test_legacy_workspace_without_proof_does_not_inherit_current_claim(
        self,
    ) -> None:
        workspace = self.store._capture_workspace()
        workspace["metadata"].pop("deliveryProof", None)
        workspace["metadata"].pop("canonicalHead", None)
        self.store.set_meta(
            "deliveryProof",
            {"commands": ["stale"], "taskOperations": [],
             "durationOperations": [], "autoStartOperations": [],
             "selectedTaskOperations": []},
        )
        self.store.set_meta("canonicalHead", {"wallMs": 3, "counter": 0})
        with self.store._immediate_transaction():
            self.store._restore_workspace(workspace)
        self.assertEqual(
            self.store.delivery_proof(),
            {"commands": [], "taskOperations": [],
             "durationOperations": [], "autoStartOperations": [],
             "selectedTaskOperations": []},
        )
        self.assertIsNone(self.store.canonical_head())

    def test_reset_deletes_account_proof_without_resurrecting_on_room_return(
        self,
    ) -> None:
        self.store.set_meta(
            "deliveryProof",
            {"commands": ["account-command"], "taskOperations": [],
             "durationOperations": [], "autoStartOperations": [],
             "selectedTaskOperations": []},
        )
        self.store.set_meta("canonicalHead", {"wallMs": 5, "counter": 0})
        self.store.reset_account_data()
        self.assertEqual(
            self.store.delivery_proof(),
            {"commands": [], "taskOperations": [],
             "durationOperations": [], "autoStartOperations": [],
             "selectedTaskOperations": []},
        )
        self.assertIsNone(self.store.canonical_head())
        self.store.create_iroh_room(bytes(range(32)))
        self.store.leave_iroh_room()
        self.assertEqual(
            self.store.delivery_proof(),
            {"commands": [], "taskOperations": [],
             "durationOperations": [], "autoStartOperations": [],
             "selectedTaskOperations": []},
        )

    def test_room_reset_clears_only_account_owned_return_proof(self) -> None:
        for account_owned in (False, True):
            with self.subTest(account_owned=account_owned):
                central = {
                    "commands": ["central-command"], "taskOperations": [],
                    "durationOperations": [], "autoStartOperations": [],
                    "selectedTaskOperations": [],
                }
                room = {
                    "commands": [], "taskOperations": [],
                    "durationOperations": [], "autoStartOperations": [],
                    "selectedTaskOperations": [],
                }
                snapshot = self.store.get_meta("snapshot")
                snapshot["user"] = {"id": "account"} if account_owned else None
                self.store.set_meta("snapshot", snapshot)
                self.store.set_meta("deliveryProof", central)
                self.store.create_iroh_room(bytes([int(account_owned)]) * 32)
                self.store.set_meta("deliveryProof", room)
                self.store.reset_account_data()
                self.assertEqual(self.store.delivery_proof(), room)
                self.store.leave_iroh_room()
                if account_owned:
                    self.assertEqual(
                        self.store.delivery_proof(),
                        {domain: [] for domain in central},
                    )
                else:
                    self.assertEqual(self.store.delivery_proof(), central)
                self.store.set_replication_mode("iroh")
                self.assertEqual(self.store.delivery_proof(), room)
                self.store.leave_iroh_room()

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

    def test_peer_secret_is_removed_when_sql_upsert_fails(self) -> None:
        with self.assertRaises(Exception):
            self.store.upsert_iroh_peer(
                "missing-room",
                "endpoint-1",
                "ticket-1",
                None,
                None,
            )

        self.assertNotIn(
            self.store._peer_ticket_key("missing-room", "endpoint-1"),
            self.secrets.values,
        )

    def test_peer_secret_is_restored_when_transaction_commit_fails(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        self.store.upsert_iroh_peer(
            room_id,
            "endpoint-1",
            "ticket-before",
            None,
            None,
        )
        transactions = self.store._replication_storage._transactions
        original_dependencies = transactions._dependencies

        @contextmanager
        def failing_transaction():
            self.store.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            finally:
                self.store.connection.rollback()
            raise OSError("commit failed")

        transactions._dependencies = replace(
            original_dependencies,
            immediate_transaction=failing_transaction,
        )
        try:
            with self.assertRaisesRegex(OSError, "commit failed"):
                self.store.upsert_iroh_peer(
                    room_id,
                    "endpoint-1",
                    "ticket-after",
                    None,
                    None,
                )
        finally:
            transactions._dependencies = original_dependencies

        self.assertEqual(
            self.secrets.load(self.store._peer_ticket_key(room_id, "endpoint-1")),
            b"ticket-before",
        )

    def test_join_restores_peer_secret_when_room_secret_save_fails(self) -> None:
        from pomodorough.iroh_protocol import room_id_for_secret

        room_secret = bytes(range(32))
        room_id = room_id_for_secret(room_secret)
        self.secrets.fail_save_key = self.store._room_secret_key(room_id)

        with self.assertRaisesRegex(OSError, "secret store unavailable"):
            self.store.prepare_iroh_join(
                room_id,
                room_secret,
                "Joined room",
                "endpoint-1",
                "ticket-1",
            )

        self.assertNotIn(
            self.store._peer_ticket_key(room_id, "endpoint-1"),
            self.secrets.values,
        )
        self.assertIsNone(self.store.iroh_room(room_id))

    def test_discard_restores_every_secret_when_later_delete_fails(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        self.store.upsert_iroh_peer(
            room_id,
            "endpoint-1",
            "ticket-1",
            None,
            None,
        )
        self.store.leave_iroh_room()
        room_key = self.store._room_secret_key(room_id)
        peer_key = self.store._peer_ticket_key(room_id, "endpoint-1")
        before = dict(self.secrets.values)
        self.secrets.fail_delete_key = peer_key

        with self.assertRaisesRegex(OSError, "secret deletion unavailable"):
            self.store.discard_inactive_iroh_room(room_id)

        self.assertEqual(self.secrets.values, before)
        self.assertIsNotNone(self.store.iroh_room(room_id))
        self.assertIn(room_key, self.secrets.values)

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
