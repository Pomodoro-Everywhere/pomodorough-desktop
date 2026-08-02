from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.cli import _history_time, main, phase_argument
from pomodorough.core import rebuild_optimistic, task_from_title
from pomodorough.storage import Store
from pomodorough.terminal import InvalidAction


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = main(arguments, store=self.store, stdout=stdout, stderr=stderr)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_start_and_json_status(self) -> None:
        result, output, error = self.invoke("start", "long-break", "--minutes", "2")
        self.assertEqual(result, 0)
        self.assertIn("Long break | running", output)
        self.assertEqual(error, "")

        result, output, _error = self.invoke("status", "--json")
        state = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual(state["phase"], "long_break")
        self.assertEqual(state["plannedDurationMs"], 120_000)
        stored = self.store.load()
        self.assertEqual(stored["settings"]["durations"]["long_break"], 15)
        self.assertEqual(
            stored["settings"]["durationsMs"]["long_break"], 15 * 60_000
        )
        self.assertEqual(stored["pendingDurations"], [])

    def test_invalid_transition_returns_error(self) -> None:
        result, output, error = self.invoke("finish")
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("Cannot finish timer while it is idle", error)

    def test_empty_history_text_and_json(self) -> None:
        result, output, error = self.invoke("history")
        self.assertEqual((result, output, error), (0, "No completed timers.\n", ""))

        result, output, error = self.invoke("history", "--json")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), [])
        self.assertEqual(error, "")

    def test_history_rejects_limit_below_one(self) -> None:
        result, output, error = self.invoke("history", "--limit", "0")

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("History limit must be at least 1.", error)

    def test_history_uses_real_store_task_json_and_limit(self) -> None:
        task = task_from_title("Write CLI coverage")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"])

        for arguments in (
            ("start", "focus", "--minutes", "2"),
            ("finish",),
            ("clear",),
            ("start", "short-break", "--minutes", "1"),
            ("finish",),
        ):
            with self.subTest(arguments=arguments):
                result, _output, error = self.invoke(*arguments)
                self.assertEqual(result, 0)
                self.assertEqual(error, "")

        result, output, error = self.invoke("history")
        self.assertEqual(result, 0)
        self.assertIn("Focus | Write CLI coverage | 2 min | pending sync", output)
        self.assertEqual(error, "")

        result, output, error = self.invoke("history", "--json")
        history = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual([item["phase"] for item in history], ["short_break", "focus"])
        focus = history[1]
        self.assertEqual(focus["taskTitle"], "Write CLI coverage")
        self.assertEqual(focus["plannedDurationMs"], 120_000)
        self.assertTrue(focus["pending"])
        self.assertTrue(
            {
                "id",
                "timerId",
                "commandId",
                "phase",
                "status",
                "plannedDurationMs",
                "completedAt",
                "pending",
                "taskId",
                "taskTitle",
            }.issubset(focus)
        )
        self.assertEqual(error, "")

        result, output, error = self.invoke("history", "--limit", "1", "--json")
        limited = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["phase"], "short_break")
        self.assertEqual(error, "")

    def test_history_time_handles_missing_invalid_and_iso_values(self) -> None:
        self.assertEqual(_history_time(None), "unknown time")
        self.assertEqual(_history_time("not-a-time"), "not-a-time")
        self.assertRegex(
            _history_time("2026-07-25T12:34:56Z"),
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
        )

    def test_status_prints_task_and_pending_operation_counts(self) -> None:
        task = task_from_title("Prepare release")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"])
        self.store.queue_duration_operation("focus", 2 * 60_000, now_ms=2)
        self.store.set_auto_start_breaks(True, now_ms=3)
        result, _output, error = self.invoke("start", "focus")
        self.assertEqual(result, 0)
        self.assertEqual(error, "")

        result, output, error = self.invoke("status")

        self.assertEqual(result, 0)
        self.assertIn("Task: Prepare release\n", output)
        self.assertIn("1 command(s) pending sync\n", output)
        self.assertIn("1 duration preference(s) pending sync\n", output)
        self.assertIn("1 auto-start preference operation(s) pending sync\n", output)
        self.assertEqual(error, "")

    def test_invalid_phase_wraps_domain_error_for_argparse(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError) as raised:
            phase_argument("rest")

        self.assertIsInstance(raised.exception.__cause__, InvalidAction)
        self.assertIn("Unknown phase 'rest'", str(raised.exception))

    def test_owned_store_receives_expanded_path_and_closes(self) -> None:
        stored_state = self.store.load()
        raw_path = "~/owned-state.sqlite3"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("pomodorough.cli.Store", autospec=True) as store_type:
            owned_store = store_type.return_value
            owned_store.load.return_value = stored_state
            owned_store.has_pending_auto_break.return_value = False

            result = main(
                ("--data", raw_path, "status"),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(result, 0)
        store_type.assert_called_once_with(Path(raw_path).expanduser())
        owned_store.close.assert_called_once_with()
        self.assertEqual(stderr.getvalue(), "")

    def test_injected_store_remains_caller_owned(self) -> None:
        with patch.object(self.store, "close", wraps=self.store.close) as close:
            result, _output, error = self.invoke("status")

        self.assertEqual(result, 0)
        self.assertEqual(error, "")
        close.assert_not_called()

    def test_expired_focus_transition_consumes_explicit_timer_command(self) -> None:
        for action in ("start", "pause", "resume", "finish", "cancel", "clear"):
            with self.subTest(action=action):
                self.store.reset_account_data()
                self.store.set_meta("hlc", {"wallMs": 0, "counter": 0})
                self.store.set_auto_start_breaks(True, now_ms=1)
                settings = self.store.load()["settings"]
                self.store.queue_command(
                    "start",
                    None,
                    "focus",
                    {**settings["durationsMs"], "focus": 60_000},
                    now_ms=2,
                )

                result, output, error = self.invoke(action)

                self.assertEqual(result, 0)
                self.assertIn("Short break | running", output)
                self.assertEqual(error, "")
                self.assertEqual(
                    [command["type"] for command in self.store.load()["pending"]],
                    ["start", "finish", "start"],
                )

    def test_restarted_auto_break_transition_consumes_explicit_command(self) -> None:
        for action in ("start", "pause", "resume", "finish", "cancel", "clear"):
            with self.subTest(action=action):
                self.store.reset_account_data()
                self.store.set_meta("hlc", {"wallMs": 0, "counter": 0})
                self.store.set_auto_start_breaks(True, now_ms=1)
                settings = self.store.load()["settings"]
                self.store.queue_command(
                    "start", None, "focus", settings["durationsMs"], now_ms=2
                )
                running, _history = rebuild_optimistic(
                    None, [], self.store.load()["pending"]
                )
                self.store.queue_command(
                    "finish", running, "focus", settings["durationsMs"], now_ms=3
                )

                result, output, error = self.invoke(action)

                self.assertEqual(result, 0)
                self.assertIn("Short break | running", output)
                self.assertEqual(error, "")
                self.assertEqual(
                    [command["type"] for command in self.store.load()["pending"]],
                    ["start", "finish", "start"],
                )

    def test_pending_resolution_status_is_safe_and_mutation_is_action_error(
        self,
    ) -> None:
        self.store.prepare_resolution({"id": "user-1"}, 4, "merge")

        result, output, error = self.invoke("status")

        self.assertEqual(result, 0)
        self.assertIn("Account history resolution pending", output)
        self.assertEqual(error, "")

        result, output, error = self.invoke("start")
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("Resolve pending account history", error)


if __name__ == "__main__":
    unittest.main()
