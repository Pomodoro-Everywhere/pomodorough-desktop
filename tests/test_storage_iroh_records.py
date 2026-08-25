from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from pomodorough.iroh_protocol import ImmutableConflict
from pomodorough.storage import Store


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class IrohRecordPersistenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(path, iroh_secret_store=MemorySecretStore())
        self.room_id = self.store.create_iroh_room(bytes(range(32)))
        self.command = self.store.queue_command(
            "start",
            None,
            "focus",
            self.store.load()["settings"]["durationsMs"],
            now_ms=1_786_000_000_000,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_inventory_cursor_pages_in_canonical_order_without_skips(self) -> None:
        first, cursor = self.store.iroh_inventory(self.room_id, None, 1)
        second, final_cursor = self.store.iroh_inventory(self.room_id, cursor, 1)

        self.assertEqual(first[0]["domain"], "genesis")
        self.assertEqual(second[0]["id"], self.command["id"])
        self.assertIsNotNone(cursor)
        self.assertIsNone(final_cursor)
        self.assertEqual(
            self.store.iroh_operations(self.room_id, first + second),
            self.store.iroh_operations(self.room_id, [*first, *second]),
        )

    def test_inventory_rejects_non_integer_limits_and_malformed_cursors(self) -> None:
        for limit in (True, 0, 1025):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                ValueError, "inventory limit"
            ):
                self.store.iroh_inventory(self.room_id, None, limit)
        for cursor in ("missing-separator", "too\0many\0parts", 4):
            with self.subTest(cursor=cursor), self.assertRaisesRegex(
                ValueError, "cursor is invalid"
            ):
                self.store.iroh_inventory(self.room_id, cursor, 1)  # type: ignore[arg-type]

    def test_operation_lookup_rejects_empty_duplicate_and_unknown_references(self) -> None:
        reference = {"domain": "timer", "id": self.command["id"]}
        invalid = (
            ([], ValueError, "1 through 256"),
            ([reference, reference], ValueError, "duplicate references"),
            ([{"domain": "timer", "id": "missing"}], KeyError, "not found"),
        )
        for references, error, message in invalid:
            with self.subTest(references=references), self.assertRaisesRegex(error, message):
                self.store.iroh_operations(self.room_id, references)

    def test_advertised_digest_mismatch_is_rejected_before_insertion(self) -> None:
        record = self.store.iroh_operations(
            self.room_id, [{"domain": "timer", "id": self.command["id"]}]
        )[0]
        before = self.store.iroh_room(self.room_id)["operationCount"]

        with self.assertRaisesRegex(ValueError, "advertised inventory digests"):
            self.store.insert_remote_iroh_records(
                self.room_id,
                [record],
                {("timer", self.command["id"]): "0" * 64},
            )

        self.assertEqual(self.store.iroh_room(self.room_id)["operationCount"], before)

    def test_inventory_conflict_persists_evidence_and_blocks_replication(self) -> None:
        timer_entry = self.store.iroh_inventory(self.room_id, None, 10)[0][1]
        remote = [
            {"domain": "task", "id": "missing", "digest": "1" * 64},
            {**timer_entry, "digest": "2" * 64},
        ]

        with self.assertRaisesRegex(ImmutableConflict, "immutable-ID conflict"):
            self.store.missing_iroh_references(self.room_id, remote)

        conflict = self.store.iroh_room(self.room_id)["conflict"]
        self.assertEqual(conflict["id"], timer_entry["id"])
        self.assertEqual(conflict["receivedDigest"], "2" * 64)
        with self.assertRaisesRegex(ImmutableConflict, "requires repair"):
            self.store.insert_remote_iroh_records(
                self.room_id,
                [self.store.iroh_operations(self.room_id, [timer_entry])[0]],
            )

    def test_timer_device_sequence_cannot_be_reused_by_another_operation(self) -> None:
        reference = {"domain": "timer", "id": self.command["id"]}
        conflicting = copy.deepcopy(
            self.store.iroh_operations(self.room_id, [reference])[0]
        )
        conflicting["operation"]["id"] = "command-conflicting-sequence"
        conflicting["operation"]["timerId"] = "timer-conflicting-sequence"
        before = self.store.iroh_room(self.room_id)["operationCount"]

        with self.assertRaisesRegex(ValueError, "reuses a device sequence"):
            self.store.insert_remote_iroh_records(self.room_id, [conflicting])

        self.assertEqual(self.store.iroh_room(self.room_id)["operationCount"], before)

    def test_duplicate_batch_and_unknown_room_are_atomic_rejections(self) -> None:
        record = self.store.iroh_operations(
            self.room_id, [{"domain": "timer", "id": self.command["id"]}]
        )[0]
        before = self.store.iroh_room(self.room_id)["operationCount"]

        with self.assertRaisesRegex(ValueError, "duplicate references"):
            self.store.insert_remote_iroh_records(self.room_id, [record, record])
        with self.assertRaisesRegex(ValueError, "room does not exist"):
            self.store._iroh_record_storage.insert_locked("unknown-room", [])

        self.assertEqual(self.store.iroh_room(self.room_id)["operationCount"], before)


if __name__ == "__main__":
    unittest.main()
