from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.core import task_from_title
from pomodorough.storage import Store
from pomodorough.terminal import InvalidAction, LocalTimer
from pomodorough.tui import build_lines


class LocalTimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_terminal_lifecycle_uses_shared_command_queue(self) -> None:
        self.timer.issue("start", phase="short-break", minutes=1, now_ms=1_000)
        state = self.timer.state(now_ms=31_000)
        self.assertEqual(state["phase"], "short_break")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["remaining"], "00:30")

        self.timer.issue("pause", now_ms=31_000)
        self.timer.issue("resume", now_ms=41_000)
        self.timer.issue("finish", now_ms=51_000)

        self.assertEqual(self.timer.state(now_ms=51_000)["status"], "completed")
        self.assertEqual(len(self.timer.completed_history()), 1)
        self.assertEqual(len(self.store.load()["pending"]), 4)

    def test_elapsed_timer_is_finished_during_live_status(self) -> None:
        self.timer.issue("start", minutes=1, now_ms=1_000)
        state = self.timer.state(now_ms=61_000, auto_finish=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["remaining"], "00:00")

    def test_focus_start_keeps_selected_task(self) -> None:
        task = task_from_title("Write release notes")
        self.store.queue_task_operation("upsert", task, now_ms=500)
        state = self.store.load()
        state["settings"]["selectedTaskId"] = task["id"]
        self.store.save_settings(state["settings"])

        self.timer.issue("start", now_ms=1_000)

        current = self.timer.state(now_ms=1_000)
        self.assertEqual(current["taskId"], task["id"])
        self.assertEqual(current["taskTitle"], task["title"])

    def test_invalid_action_does_not_queue_command(self) -> None:
        with self.assertRaises(InvalidAction):
            self.timer.issue("pause")
        self.assertEqual(self.store.load()["pending"], [])

    def test_tui_lines_include_timer_and_history(self) -> None:
        self.timer.issue("start", minutes=1, now_ms=1_000)
        self.timer.issue("finish", now_ms=31_000)
        state = self.timer.state(now_ms=31_000)
        lines = build_lines(state, self.timer.completed_history(), 80)
        rendered = "\n".join(lines)
        self.assertIn("00:00", rendered)
        self.assertIn("Focus", rendered)
        self.assertIn("pending sync", rendered)


if __name__ == "__main__":
    unittest.main()
