from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pomodorough.core import rebuild_optimistic, task_from_title
from pomodorough.storage import Store
from pomodorough.uuid7 import (
    UUID7_MAX_TIMESTAMP_MS,
    UUID7_RANDOM_MAX,
    uuid7_from_parts,
    uuid7_parts,
)


class UUID7StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_all_user_mutations_use_uuid7_with_hlc_timestamp(self) -> None:
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        task = self.store.queue_task_operation(
            "upsert", task_from_title("UUID task"), now_ms=1_000
        )
        duration = self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=1_000
        )
        auto_start = self.store.set_auto_start_breaks(True, now_ms=1_000)

        operations = (command, task, duration, auto_start)
        self.assertTrue(all(uuid.UUID(item["id"]).version == 7 for item in operations))
        self.assertEqual(
            [uuid7_parts(item["id"])[0] for item in operations],
            [item["hlcWallMs"] for item in operations],
        )
        self.assertEqual(
            [item["id"] for item in operations],
            sorted(item["id"] for item in operations),
        )
        self.assertEqual(uuid.UUID(command["timerId"]).version, 4)

    def test_restart_and_physical_rollback_continue_monotonically(self) -> None:
        settings = self.store.load()["settings"]
        first = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=2_000
        )
        first_state = self.store.get_meta("lastUuidV7")
        self.store.close()
        self.store = Store(self.path)

        second = self.store.queue_task_operation(
            "upsert", task_from_title("After restart"), now_ms=1_900
        )

        self.assertEqual(uuid7_parts(first["id"])[0], 2_000)
        self.assertEqual(uuid7_parts(second["id"])[0], 2_000)
        self.assertGreater(second["id"], first["id"])
        self.assertGreater(self.store.get_meta("lastUuidV7"), first_state)

    def test_response_loss_and_restart_reuse_committed_identifier(self) -> None:
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        first_payload = self.store.sync_payload()
        persisted_state = self.store.get_meta("lastUuidV7")
        self.store.close()
        self.store = Store(self.path)

        retried_payload = self.store.sync_payload()

        self.assertEqual(retried_payload["commands"], [command])
        self.assertEqual(retried_payload, first_payload)
        self.assertEqual(self.store.get_meta("lastUuidV7"), persisted_state)

    def test_transaction_failure_rolls_back_uuid_hlc_sequence_and_queue(self) -> None:
        settings = self.store.load()["settings"]
        before = {
            "uuid": self.store.get_meta("lastUuidV7"),
            "hlc": self.store.get_meta("hlc"),
            "sequence": self.store.get_meta("deviceSequence"),
            "pending": self.store.load()["pending"],
        }

        with (
            patch.object(
                self.store,
                "_record_command_physical_time",
                side_effect=RuntimeError("injected failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected failure"),
        ):
            self.store.queue_command(
                "start", None, "focus", settings["durationsMs"], now_ms=1_000
            )

        self.assertEqual(self.store.get_meta("lastUuidV7"), before["uuid"])
        self.assertEqual(self.store.get_meta("hlc"), before["hlc"])
        self.assertEqual(self.store.get_meta("deviceSequence"), before["sequence"])
        self.assertEqual(self.store.load()["pending"], before["pending"])

    def test_missing_state_reconstructs_from_pending_uuid7(self) -> None:
        settings = self.store.load()["settings"]
        first = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        self.store.set_meta("lastUuidV7", None)
        self.store.close()
        self.store = Store(self.path)

        second = self.store.queue_task_operation(
            "upsert", task_from_title("Reconstructed"), now_ms=1_000
        )

        self.assertGreater(second["id"], first["id"])
        self.assertEqual(self.store.get_meta("lastUuidV7"), second["id"])

    def test_corrupt_or_stale_state_blocks_new_mutation_without_queue_loss(
        self,
    ) -> None:
        settings = self.store.load()["settings"]
        existing = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        before = self.store.load()["pending"]

        self.store.set_meta("lastUuidV7", "not-a-uuid")
        with self.assertRaisesRegex(ValueError, "UUIDv7 state"):
            self.store.queue_task_operation(
                "upsert", task_from_title("Blocked"), now_ms=1_001
            )
        self.assertEqual(self.store.load()["pending"], before)

        self.store.set_meta("lastUuidV7", uuid7_from_parts(999, 0))
        with self.assertRaisesRegex(ValueError, "predates"):
            self.store.queue_task_operation(
                "upsert", task_from_title("Still blocked"), now_ms=1_001
            )
        self.assertEqual(self.store.load()["pending"], before)
        self.assertEqual(self.store.load()["pending"][0]["id"], existing["id"])

    def test_tail_overflow_rolls_back_finish_and_generated_break_batch(self) -> None:
        now_ms = 2_000
        self.store.set_auto_start_breaks(True, now_ms=now_ms - 2)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms - 1
        )
        running, _history = rebuild_optimistic(None, [], [start])
        self.store.set_meta(
            "lastUuidV7", uuid7_from_parts(now_ms, UUID7_RANDOM_MAX - 1)
        )
        self.store.set_meta("hlc", {"wallMs": now_ms, "counter": 0})
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "headroom"):
            self.store.queue_command(
                "finish",
                running,
                "focus",
                settings["durationsMs"],
                now_ms=now_ms,
                generate_auto_break=True,
            )

        self.assertEqual(self.store.load(), before)
        self.assertEqual(
            self.store.get_meta("lastUuidV7"),
            uuid7_from_parts(now_ms, UUID7_RANDOM_MAX - 1),
        )

    def test_finish_and_generated_break_reserve_consecutive_uuid7_values(self) -> None:
        now_ms = 2_000
        self.store.set_auto_start_breaks(True, now_ms=now_ms - 2)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms - 1
        )
        running, _history = rebuild_optimistic(None, [], [start])

        self.store.queue_command(
            "finish",
            running,
            "focus",
            settings["durationsMs"],
            now_ms=now_ms,
            generate_auto_break=True,
        )

        finish, generated = self.store.load()["pending"][-2:]
        finish_parts = uuid7_parts(finish["id"])
        generated_parts = uuid7_parts(generated["id"])
        self.assertEqual(finish_parts[0], now_ms)
        self.assertEqual(generated_parts, (now_ms, finish_parts[1] + 1))
        self.assertEqual(self.store.get_meta("lastUuidV7"), generated["id"])

    def test_uuid7_timestamp_overflow_rolls_back_mutation(self) -> None:
        settings = self.store.load()["settings"]
        self.store.set_meta("hlc", {"wallMs": UUID7_MAX_TIMESTAMP_MS, "counter": 0})
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.store.queue_command(
                "start",
                None,
                "focus",
                settings["durationsMs"],
                now_ms=UUID7_MAX_TIMESTAMP_MS + 1,
            )

        self.assertEqual(self.store.load(), before)
        self.assertIsNone(self.store.get_meta("lastUuidV7"))

    def test_mixed_legacy_uuid4_queue_is_not_rewritten(self) -> None:
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        legacy_id = str(uuid.uuid4())
        command["id"] = legacy_id
        self.store.connection.execute(
            "UPDATE pending_commands SET id = ?, payload = ? WHERE device_sequence = ?",
            (
                legacy_id,
                json.dumps(command, separators=(",", ":")),
                command["deviceSequence"],
            ),
        )
        self.store.connection.commit()
        self.store.set_meta("lastUuidV7", None)

        operation = self.store.queue_task_operation(
            "upsert", task_from_title("Mixed queue"), now_ms=1_001
        )

        self.assertEqual(self.store.load()["pending"][0]["id"], legacy_id)
        self.assertEqual(uuid.UUID(legacy_id).version, 4)
        self.assertEqual(uuid.UUID(operation["id"]).version, 7)


if __name__ == "__main__":
    unittest.main()
