from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.core import task_from_title
from pomodorough.storage import Store
from pomodorough.terminal import InvalidAction, LocalTimer
from pomodorough.tui import build_lines, handle_key


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
        self.assertEqual(self.timer.selected_phase, "focus")
        self.assertEqual(len(self.timer.completed_history()), 1)
        self.assertEqual(len(self.store.load()["pending"]), 4)

    def test_elapsed_timer_is_finished_during_live_status(self) -> None:
        self.timer.issue("start", minutes=1, now_ms=1_000)
        state = self.timer.state(now_ms=61_000, auto_finish=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["remaining"], "00:00")
        self.assertEqual(self.timer.selected_phase, "short_break")

    def test_live_status_deadline_uses_monotonic_time_across_wall_jump(self) -> None:
        physical_ms = 1_800_000_000_000
        with (
            patch("pomodorough.storage.time.time", return_value=physical_ms / 1_000),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=10_000_000_000
            ),
        ):
            self.timer.issue("start", minutes=1)
            self.assertEqual(self.timer.state()["remainingMs"], 60_000)

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms + 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=40_000_000_000
            ),
        ):
            state = self.timer.state(auto_finish=True)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["elapsedMs"], 30_000)

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms - 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=70_000_000_000
            ),
        ):
            state = self.timer.state(auto_finish=True)
        self.assertEqual(state["status"], "completed")

    def test_completion_advances_through_repeating_pomodoro_cycle(self) -> None:
        expected_breaks = [
            "short_break",
            "short_break",
            "short_break",
            "long_break",
            "short_break",
        ]
        now_ms = 1_000
        self.timer.issue("start", now_ms=now_ms)

        for index, expected_break in enumerate(expected_breaks):
            now_ms += 1_000
            self.timer.issue("finish", now_ms=now_ms)
            self.assertEqual(self.timer.selected_phase, expected_break)

            now_ms += 1_000
            self.timer.primary(now_ms=now_ms)
            self.assertEqual(self.timer.state(now_ms=now_ms)["phase"], expected_break)
            now_ms += 1_000
            self.timer.issue("finish", now_ms=now_ms)
            self.assertEqual(self.timer.selected_phase, "focus")

            if index < len(expected_breaks) - 1:
                now_ms += 1_000
                self.timer.primary(now_ms=now_ms)

        completed_focus = [
            item
            for item in self.timer.completed_history()
            if item["phase"] == "focus"
        ]
        self.assertEqual(len(completed_focus), 5)

    def test_signed_in_terminal_auto_starts_break_without_sync(self) -> None:
        self.store.set_user({"id": "user-1"})
        self.store.set_auto_start_breaks(True, now_ms=100)

        self.timer.issue("start", minutes=1, now_ms=1_000)
        self.timer.issue("finish", now_ms=61_000)

        state = self.timer.state(now_ms=61_000)
        self.assertEqual((state["phase"], state["status"]), ("short_break", "running"))
        self.assertEqual(
            [command["type"] for command in self.store.load()["pending"]],
            ["start", "finish", "start"],
        )

    def test_cancel_resets_timer_and_preserves_cancelled_history(self) -> None:
        self.timer.issue("start", minutes=1, now_ms=1_000)
        self.timer.issue("pause", now_ms=31_000)
        self.timer.issue("cancel", now_ms=31_000)

        state = self.timer.state(now_ms=31_000)

        self.assertEqual(state["status"], "idle")
        self.assertIsNone(state["timerId"])
        self.assertEqual(state["elapsedMs"], 0)
        self.assertEqual(state["remaining"], "25:00")
        self.assertEqual(state["progress"], 0)
        self.assertEqual([item["status"] for item in self.timer.history], ["cancelled"])
        self.assertEqual(
            [command["type"] for command in self.store.load()["pending"]],
            ["start", "pause", "cancel", "clear"],
        )

    def test_focus_start_keeps_selected_task(self) -> None:
        task = task_from_title("Write release notes")
        self.store.queue_task_operation("upsert", task, now_ms=500)
        self.store.set_selected_task_id(task["id"], now_ms=501)

        self.timer.issue("start", now_ms=1_000)

        current = self.timer.state(now_ms=1_000)
        self.assertEqual(current["taskId"], task["id"])
        self.assertEqual(current["taskTitle"], task["title"])

    def test_invalid_action_does_not_queue_command(self) -> None:
        with self.assertRaises(InvalidAction):
            self.timer.issue("pause")
        self.assertEqual(self.store.load()["pending"], [])

    def test_pending_resolution_reports_status_and_blocks_with_domain_errors(
        self,
    ) -> None:
        self.store.prepare_resolution({"id": "user-1"}, 4, "merge")
        before = self.store.load()

        state = self.timer.state(auto_finish=True)

        self.assertTrue(state["historyResolutionPending"])
        rendered = "\n".join(build_lines(state, [], 80))
        self.assertIn("history resolution pending", rendered)
        actions = (
            lambda: self.timer.issue("start"),
            lambda: self.timer.select_phase("short-break"),
            lambda: self.timer.adjust_duration(1),
            lambda: handle_key(self.timer, ord("+")),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(InvalidAction, "pending account history"):
                    action()
                self.assertEqual(self.store.load(), before)

    def test_pending_resolution_status_does_not_auto_finish_elapsed_timer(
        self,
    ) -> None:
        self.timer.issue("start", minutes=1, now_ms=1_000)
        self.store.prepare_resolution({"id": "user-1"}, 4, "merge")

        state = self.timer.state(now_ms=61_000, auto_finish=True)

        self.assertEqual(state["status"], "running")
        self.assertEqual(state["remainingMs"], 0)
        self.assertTrue(state["historyResolutionPending"])
        self.assertEqual(len(self.store.load()["pending"]), 1)

    def test_pending_resolution_status_preserves_unavailable_selection(self) -> None:
        task = task_from_title("Pending selection")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"], now_ms=2)
        self.store.queue_task_operation("delete", task, now_ms=3)
        self.store.prepare_resolution({"id": "user-1"}, 4, "merge")

        state = self.timer.state()

        self.assertTrue(state["historyResolutionPending"])
        self.assertEqual(self.timer.settings["selectedTaskId"], task["id"])
        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], task["id"]
        )
        self.assertEqual(
            [
                operation["taskId"]
                for operation in self.store.load()["pendingSelectedTasks"]
            ],
            [task["id"]],
        )

    def test_tui_duration_adjustment_queues_compacted_preference(self) -> None:
        handle_key(self.timer, ord("+"))
        handle_key(self.timer, ord("+"))

        loaded = self.store.load()
        self.assertEqual(loaded["settings"]["durations"]["focus"], 27)
        self.assertEqual(loaded["settings"]["durationsMs"]["focus"], 27 * 60_000)
        self.assertEqual(len(loaded["pendingDurations"]), 1)
        self.assertEqual(loaded["pendingDurations"][0]["durationMs"], 27 * 60_000)
        rendered = "\n".join(
            build_lines(self.timer.state(now_ms=1_000), [], 80)
        )
        self.assertIn("1 duration preference(s) pending sync", rendered)

    def test_duration_edit_does_not_change_active_timer_plan(self) -> None:
        self.timer.issue("start", now_ms=1_000)
        planned = self.timer.state(now_ms=1_000)["plannedDurationMs"]

        self.timer.adjust_duration(1)

        self.assertEqual(
            self.timer.state(now_ms=1_000)["plannedDurationMs"], planned
        )
        self.assertEqual(self.store.load()["settings"]["durations"]["focus"], 26)

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
