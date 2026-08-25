from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from iroh_protocol_cases import genesis_record

from pomodorough.core import (
    elapsed_ms,
    parse_timestamp_ms,
    project_auto_start_breaks,
    project_durations,
    rebuild_tasks,
    task_from_title,
)
from pomodorough.iroh_protocol import IrohProtocolError, validate_record
from pomodorough.storage import MAX_SAFE_INTEGER, Store
from pomodorough.storage_canonical_reconciliation import (
    validated_reconciliation_dependencies,
    validated_reconciliation_id_sets,
)
from pomodorough.uuid7 import UUID7_RANDOM_MAX, reserve_uuid7, uuid7_from_parts


class PureBranchMatrixTests(unittest.TestCase):
    def test_uuid7_rejects_typed_bounds_and_entropy_exhaustion(self) -> None:
        for timestamp, random_value in ((True, 0), (-1, 0), (0, True), (0, -1)):
            with self.subTest(timestamp=timestamp, random=random_value), self.assertRaises(ValueError):
                uuid7_from_parts(timestamp, random_value)
        for count in (True, 0, UUID7_RANDOM_MAX + 2):
            with self.subTest(count=count), self.assertRaises(ValueError):
                reserve_uuid7(1, count, None)
        with self.assertRaisesRegex(ValueError, "entropy lacks"):
            reserve_uuid7(1, 2, None, entropy=lambda _size: b"\xff" * 10)

    def test_compatibility_projections_ignore_invalid_and_accept_valid_operations(self) -> None:
        task = task_from_title("Matrix task")
        operations = [
            {"taskId": "", "type": "upsert", "title": "ignored"},
            {"taskId": task["id"], "type": "upsert", "title": 1},
            {"taskId": task["id"], "type": "upsert", "title": "Wrong"},
            {"taskId": task["id"], "type": "upsert", "title": task["title"]},
        ]
        self.assertEqual(rebuild_tasks([], operations), [task])
        self.assertTrue(project_auto_start_breaks(False, [{"enabled": "yes"}, {"enabled": True}]))
        durations = project_durations({}, [{"phase": "focus", "durationMs": True}, {"phase": "focus", "durationMs": 1_800_000}])
        self.assertEqual(durations["focus"], 1_800_000)
        self.assertIsNone(parse_timestamp_ms("2025-01-01T00:00:00"))
        self.assertEqual(elapsed_ms(None, 1), 0)

    def test_storage_scalar_validators_cover_malformed_shapes_and_bounds(self) -> None:
        for value in (True, "60000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Store._duration_ms(value)
        for value in (True, MAX_SAFE_INTEGER + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Store._signed_safe_integer(value, "value")
        with self.assertRaises(ValueError):
            Store._server_clock_sample([])
        sample = {"offsetMs": 0, "uncertaintyMs": 1, "acquiredPhysicalMs": 100, "acquiredMonotonicMs": 100, "acquiredTrustedMs": 100}
        with self.assertRaises(ValueError):
            Store._server_clock_sample({**sample, "extra": 1})
        self.assertIsNone(Store._projected_trusted_time(sample, 100, 99))
        with self.assertRaises(ValueError):
            Store._logical_clock("bad")
        with self.assertRaises(ValueError):
            Store._operation_clock({"occurredAt": "bad", "hlcWallMs": 1, "hlcCounter": 0})

    def test_reconciliation_id_sets_reject_bad_types_duplicates_and_unavailable_ids(self) -> None:
        canonical = {"acknowledgements": []}
        local = {"a": {}, "b": {}}
        invalid = ValueError("invalid")
        cases = (
            {"droppedTimerOperationIds": "bad", "promotedTimerOperationIds": [], "droppedTimerIds": []},
            {"droppedTimerOperationIds": ["a", "a"], "promotedTimerOperationIds": [], "droppedTimerIds": []},
            {"droppedTimerOperationIds": ["missing"], "promotedTimerOperationIds": [], "droppedTimerIds": []},
            {"droppedTimerOperationIds": ["a"], "promotedTimerOperationIds": ["a"], "droppedTimerIds": []},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validated_reconciliation_id_sets(value, local, canonical, invalid)

    def test_reconciliation_dependencies_validate_shape_graph_and_generated_dates(self) -> None:
        commands = [{"id": "a"}, {"id": "b"}]
        invalid = ValueError("invalid")
        cases = (
            {},
            ["bad"],
            [{"operationId": "a", "dependsOnOperationId": "a"}],
            [{"operationId": "missing", "dependsOnOperationId": "a"}],
            [{"operationId": "b", "dependsOnOperationId": "a", "generatedBreak": True, "sourceDayStart": 1, "sourceDayEnd": "x"}],
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validated_reconciliation_dependencies(value, commands, invalid)
        valid = [{"operationId": "b", "dependsOnOperationId": "a", "generatedBreak": True, "sourceDayStart": "2025-01-01", "sourceDayEnd": "2025-01-02"}]
        self.assertEqual(validated_reconciliation_dependencies(valid, commands, invalid), valid)

    def test_genesis_timer_intent_history_task_and_record_size_fail_closed(self) -> None:
        base = genesis_record()
        mutations = (
            lambda operation: operation.update(canonicalTimer={"id": "bad"}),
            lambda operation: operation.update(canonicalTimer={"id": "timer-identity-0001", "phase": "focus", "status": "idle", "plannedDurationMs": 1_500_000, "elapsedAtAnchorMs": 0, "anchorAt": None, "lastIntent": "bad"}),
            lambda operation: operation.update(tasks=[{"id": "task-identity-0001"}]),
            lambda operation: operation.update(selectedTaskId="bad"),
        )
        for mutate in mutations:
            record = copy.deepcopy(base)
            mutate(record["operation"])
            with self.subTest(record=record), self.assertRaises(IrohProtocolError):
                validate_record(record)
        huge = copy.deepcopy(base)
        huge["operation"]["tasks"] = [{"id": "task-" + "x" * 70_000, "title": "x" * 70_000}]
        with self.assertRaises(IrohProtocolError):
            validate_record(huge)


class StoreBranchMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "matrix.sqlite3", iroh_secret_store=Mock())

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_public_mutation_validation_and_ownership_boundaries(self) -> None:
        for operation in (
            lambda: self.store.set_selected_phase("unknown"),
            lambda: self.store.set_auto_start_breaks(1),
            lambda: self.store.set_selected_task_id(""),
            lambda: self.store.queue_task_operation("rename", {"id": "x", "title": "x"}),
            lambda: self.store.queue_duration_operation("unknown", 60_000),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
        self.assertFalse(self.store.owns_timer(None))
        self.store._set_meta("replicationMode", "iroh")
        self.assertFalse(self.store.owns_timer({"id": "timer", "startedByDeviceId": "other"}))

    def test_persisted_shape_and_clock_guards(self) -> None:
        self.store._set_meta("commandPhysicalTimes", [])
        with self.assertRaises(ValueError):
            self.store._command_physical_times()
        with self.assertRaises(ValueError):
            self.store._pending_object("[]", "operation")
        self.store._set_meta("deviceId", None)
        with self.assertRaises(ValueError):
            self.store._preflight_pending_queues()
        with self.assertRaises(ValueError):
            Store._validate_pending_clock_coverage(([{"hlcWallMs": 2, "hlcCounter": 0}],), (1, 0), True)
        with self.assertRaises(ValueError):
            self.store._reserve_generation(100, sequence_count=-1)

    def test_projection_contract_guards_invalid_shapes(self) -> None:
        base = {"canonicalTimer": None, "history": [], "tasks": [], "durationsMs": {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}, "autoStartBreaks": False, "selectedTaskId": None}
        mutations = (
            {**base, "canonicalTimer": []},
            {**base, "history": [1]},
            {**base, "autoStartBreaks": 0},
            {**base, "selectedTaskId": 1},
            {**base, "selectedTaskId": "missing"},
        )
        for projection in mutations:
            with self.subTest(projection=projection), self.assertRaises(ValueError):
                self.store._validated_projection_state(projection, context="matrix")
        with self.assertRaises(ValueError):
            self.store._project_operation(self.store._normalize_settings({}), state=self.store.load())

    def test_physical_projection_retains_unusable_values(self) -> None:
        self.assertEqual(self.store._physical_timestamp(123), 123)
        self.assertEqual(self.store._physical_timestamp("bad"), "bad")
        snapshot = {"canonicalTimer": None, "history": ["bad", {"id": "ok"}]}
        self.assertEqual(self.store._physical_snapshot(snapshot)["history"][0], "bad")
        command = {"id": "missing"}
        self.assertEqual(self.store._physical_pending_commands([command]), [command])


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class ReplicationStorageBranchMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.secrets = MemorySecrets()
        self.store = Store(
            Path(self.temp.name) / "replication.sqlite3",
            iroh_secret_store=self.secrets,  # type: ignore[arg-type]
        )
        self.rooms = self.store._replication_storage._rooms
        self.transactions = self.store._replication_storage._transactions

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_room_creation_rejects_names_duplicates_and_invalid_join_metadata(self) -> None:
        for name in ("", "x" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.rooms.create_room(bytes(32), name)
        room_id = self.rooms.create_room(bytes(32), "Matrix")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.rooms.create_room(bytes(32), "Matrix")
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.rooms.prepare_join("bad", bytes(32), None, "peer", "ticket")
        self.assertEqual(self.rooms.room_secret(room_id), bytes(32))

    def test_existing_join_checks_secret_conflict_and_reuses_valid_room(self) -> None:
        secret = bytes(range(32))
        room_id = self.rooms.create_room(secret)
        self.rooms.prepare_join(room_id, secret, None, "peer-a", "ticket-a")
        self.assertEqual(self.transactions.peers(room_id)[0]["endpointTicket"], "ticket-a")
        with self.assertRaisesRegex(ValueError, "credentials"):
            self.rooms._validate_existing_join_locked(
                room_id, bytes(reversed(range(32)))
            )
        self.store.connection.execute(
            "UPDATE iroh_rooms SET conflict = '{}' WHERE room_id = ?", (room_id,)
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "repair"):
            self.rooms.prepare_join(room_id, secret, None, "peer", "ticket")

    def test_discard_and_activation_fail_closed_for_active_conflict_and_missing_genesis(self) -> None:
        room_id = self.rooms.create_room(bytes(32))
        with self.assertRaisesRegex(ValueError, "Active"):
            self.rooms.discard_inactive_room(room_id)
        self.rooms.set_mode("offline")
        self.store.connection.execute(
            "UPDATE iroh_rooms SET conflict = '{}' WHERE room_id = ?", (room_id,)
        )
        self.store.connection.commit()
        self.rooms.discard_inactive_room(room_id)
        self.assertIsNotNone(self.rooms.room(room_id))
        self.store.connection.execute(
            "UPDATE iroh_rooms SET conflict = NULL WHERE room_id = ?", (room_id,)
        )
        self.store.connection.execute(
            "DELETE FROM iroh_records WHERE room_id = ?", (room_id,)
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "no valid genesis"):
            self.rooms.activate_joined_room(room_id)
        self.rooms.discard_inactive_room(room_id)
        self.assertIsNone(self.rooms.room(room_id))

    def test_mode_and_secret_guards_cover_missing_and_corrupt_state(self) -> None:
        with self.assertRaises(ValueError):
            self.rooms.set_mode("peer-to-peer")
        self.rooms.set_mode("centralized")
        with self.assertRaisesRegex(ValueError, "No Iroh room"):
            self.rooms.leave_room()
        with self.assertRaisesRegex(ValueError, "before selecting"):
            self.rooms.set_mode("iroh")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.rooms.room_secret("missing")
        room_id = self.rooms.create_room(bytes(32))
        key = self.store._room_secret_key(room_id)
        self.secrets.values[key] = b"short"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.rooms.room_secret(room_id)

    def test_transaction_boundaries_reject_empty_batches_missing_rooms_and_bad_peers(self) -> None:
        self.store._set_meta("replicationMode", "offline")
        self.store.connection.commit()
        self.assertFalse(self.transactions.capture_local_records())
        self.assertFalse(self.transactions.project_expiry(100))
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.transactions.insert_remote_records("room", [])
        with patch.object(
            self.transactions._dependencies.projection,
            "project_room",
            return_value=Mock(),
        ), self.assertRaisesRegex(ValueError, "workspace is missing"):
            self.transactions._refresh_workspace("missing")
        self.store.connection.rollback()
        cases = (("", "ticket", None), ("peer", "", None), ("peer", "ticket", ""))
        for endpoint, ticket, name in cases:
            with self.subTest(endpoint=endpoint, ticket=ticket, name=name), self.assertRaises(ValueError):
                self.transactions.upsert_peer("room", endpoint, ticket, None, name)

    def test_peer_capabilities_fail_closed_when_missing_or_not_utf8(self) -> None:
        room_id = self.rooms.create_room(bytes(32))
        self.transactions.upsert_peer(room_id, "peer-a", "ticket-a", None, None)
        key = self.store._peer_ticket_key(room_id, "peer-a")
        self.secrets.values.pop(key)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.transactions.peers(room_id)
        self.secrets.values[key] = b"\xff"
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.transactions.peers(room_id)
        self.store.connection.execute(
            "DELETE FROM iroh_peers WHERE room_id = ?", (room_id,)
        )
        self.assertEqual(self.transactions.peers(room_id), [])


if __name__ == "__main__":
    unittest.main()
