from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.core import rebuild_optimistic, task_from_title
from pomodorough.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_command_is_durable_before_reducer(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start", None, "focus", settings["durations"], now_ms=1_784_548_800_000
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [queued])
        timer, _history = rebuild_optimistic(None, [], loaded["pending"])
        self.assertEqual(timer["status"], "running")

    def test_sync_acknowledgement_removes_command(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start", None, "focus", settings["durations"], now_ms=1_784_548_800_000
        )
        self.store.apply_sync(
            {
                "acknowledgements": [
                    {"commandId": queued["id"], "outcome": "applied", "reason": ""}
                ],
                "revision": 1,
                "canonicalTimer": {
                    "id": queued["timerId"],
                    "phase": "focus",
                    "status": "running",
                    "plannedDurationMs": 1_500_000,
                    "elapsedAtAnchorMs": 0,
                    "anchorAt": queued["occurredAt"],
                },
                "history": [],
                "serverHlcWallMs": queued["hlcWallMs"],
            }
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["snapshot"]["revision"], 1)

    def test_task_operation_is_durable_and_reconciles(self) -> None:
        task = task_from_title("Write release notes")
        operation = self.store.queue_task_operation(
            "upsert", task, now_ms=1_784_548_800_000
        )
        payload = self.store.sync_payload()
        self.assertEqual(payload["taskOperations"], [operation])
        self.assertEqual(payload["commands"], [])

        self.store.apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [
                    {
                        "operationId": operation["id"],
                        "outcome": "applied",
                        "reason": "",
                    }
                ],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [task],
                "serverHlcWallMs": operation["hlcWallMs"],
            }
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

    def test_focus_start_has_task_but_break_start_does_not(self) -> None:
        settings = self.store.load()["settings"]
        task = task_from_title("Release")
        focus = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durations"],
            task["id"],
            now_ms=1_784_548_800_000,
        )
        self.assertEqual(focus["taskId"], task["id"])

        self.store.apply_sync(
            {
                "acknowledgements": [
                    {"commandId": focus["id"], "outcome": "applied", "reason": ""}
                ],
                "taskAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "serverHlcWallMs": focus["hlcWallMs"],
            }
        )
        break_start = self.store.queue_command(
            "start",
            None,
            "short_break",
            settings["durations"],
            task["id"],
            now_ms=1_784_548_801_000,
        )
        self.assertNotIn("taskId", break_start)

    def test_reset_clears_account_tasks_and_selection(self) -> None:
        task = task_from_title("Release")
        self.store.queue_task_operation("upsert", task, now_ms=1_784_548_800_000)
        settings = self.store.load()["settings"]
        settings["selectedTaskId"] = task["id"]
        self.store.save_settings(settings)

        self.store.reset_account_data()
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [])
        self.assertIsNone(loaded["settings"]["selectedTaskId"])


if __name__ == "__main__":
    unittest.main()
