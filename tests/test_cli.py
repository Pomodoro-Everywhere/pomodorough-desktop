from __future__ import annotations

import argparse
import io
import json
import sqlite3
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

    def invoke(
        self, *arguments: str, locale: str | None = None
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = main(
            arguments, store=self.store, stdout=stdout, stderr=stderr, locale=locale
        )
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

    def test_completed_json_keeps_source_timer_and_separate_next_display(self) -> None:
        self.assertEqual(self.invoke("start", "focus", "--minutes", "1")[0], 0)
        self.assertEqual(self.invoke("finish")[0], 0)

        result, output, error = self.invoke("status", "--json")
        state = json.loads(output)

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["phase"], "focus")
        self.assertEqual(state["plannedDurationMs"], 60_000)
        self.assertIsNotNone(state["timerId"])
        self.assertEqual(state["display"]["phase"], "short_break")
        self.assertEqual(state["display"]["remaining"], "05:00")

    def test_status_completes_overdue_pause_then_resume_projection(self) -> None:
        settings = self.store.load()["settings"]
        durations_ms = dict(settings["durationsMs"])
        durations_ms["focus"] = 60_000
        self.store.queue_command(
            "start", None, "focus", durations_ms, now_ms=1_000
        )
        running = self.store.projected_state(now_ms=1_000).canonical_timer
        self.store.queue_command(
            "pause", running, "focus", durations_ms, now_ms=70_000
        )
        paused = self.store.projected_state(now_ms=70_000).canonical_timer
        self.store.queue_command(
            "resume", paused, "focus", durations_ms, now_ms=80_000
        )

        result, output, error = self.invoke("status", "--json")
        state = json.loads(output)
        history_result, history_output, history_error = self.invoke(
            "history", "--json"
        )
        history = json.loads(history_output)

        self.assertEqual((result, error), (0, ""))
        self.assertEqual((history_result, history_error), (0, ""))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(
            (history[0]["id"], history[0]["timerId"]),
            (state["timerId"], state["timerId"]),
        )

    def test_invalid_transition_returns_error(self) -> None:
        result, output, error = self.invoke("finish")
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("Cannot finish timer while it is idle", error)

    def test_invalid_transition_json_returns_versioned_error_only(self) -> None:
        result, output, error = self.invoke("pause", "--json")

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertEqual(
            json.loads(error),
            {
                "version": 1,
                "code": "invalid_action",
                "type": "InvalidAction",
                "message": "Cannot pause timer while it is idle.",
            },
        )
        self.assertEqual(error.count("\n"), 1)

    def test_argument_validation_json_returns_versioned_error_only(self) -> None:
        result, output, error = self.invoke(
            "start", "focus", "--minutes", "many", "--json"
        )

        payload = json.loads(error)
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["code"], "invalid_arguments")
        self.assertEqual(payload["type"], "ArgumentError")
        self.assertIn("invalid int value", payload["message"])
        self.assertEqual(error.count("\n"), 1)

    def test_storage_failure_json_returns_versioned_error_only(self) -> None:
        with patch.object(
            self.store, "load", side_effect=sqlite3.OperationalError("disk unavailable")
        ):
            result, output, error = self.invoke("status", "--json")

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertEqual(
            json.loads(error),
            {
                "version": 1,
                "code": "storage_error",
                "type": "StorageError",
                "message": "disk unavailable",
            },
        )
        self.assertEqual(error.count("\n"), 1)

    def test_corrupt_persisted_json_returns_versioned_storage_error_only(self) -> None:
        error = json.JSONDecodeError("invalid persisted JSON", "{", 1)
        with patch.object(self.store, "load", side_effect=error):
            result, output, stderr = self.invoke("status", "--json")

        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["code"], "storage_error")
        self.assertEqual(payload["type"], "StorageError")
        self.assertIn("invalid persisted JSON", payload["message"])
        self.assertEqual(stderr.count("\n"), 1)

    def test_store_open_failure_json_returns_versioned_error_only(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("pomodorough.cli.Store", side_effect=OSError("cannot open store")):
            result = main(("status", "--json"), stdout=stdout, stderr=stderr)

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "version": 1,
                "code": "storage_error",
                "type": "StorageError",
                "message": "cannot open store",
            },
        )

    def test_cancel_resets_timer_and_preserves_cancelled_history(self) -> None:
        self.assertEqual(self.invoke("start", "focus", "--minutes", "1")[0], 0)

        result, output, error = self.invoke("cancel")

        self.assertEqual(result, 0)
        self.assertIn("Focus | idle", output)
        self.assertEqual(error, "")
        stored = self.store.load()
        timer, history = rebuild_optimistic(
            stored["snapshot"].get("canonicalTimer"),
            stored["snapshot"].get("history", []),
            stored["pending"],
        )
        self.assertIsNone(timer)
        self.assertEqual([item["status"] for item in history], ["cancelled"])
        self.assertEqual(
            [command["type"] for command in stored["pending"]],
            ["start", "cancel", "clear"],
        )

    def test_empty_history_text_and_json(self) -> None:
        result, output, error = self.invoke("history")
        self.assertEqual(
            (result, output, error),
            (
                0,
                "No arrivals yet\n"
                "Completed, cancelled, and superseded timers appear here.\n",
                "",
            ),
        )

        result, output, error = self.invoke("history", "--json")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), [])
        self.assertEqual(error, "")

    def test_rtl_pseudolocale_localizes_human_output_but_not_json(self) -> None:
        result, output, error = self.invoke("history", locale="ar-XB")
        self.assertEqual((result, error), (0, ""))
        self.assertIn("⟦", output)

        result, output, error = self.invoke("status", "--json", locale="ar-XB")
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "idle")

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
        self.assertIn(
            "Focus | Write CLI coverage | Completed | 2 min | pending sync",
            output,
        )
        self.assertEqual(error, "")

        result, output, error = self.invoke("history", "--json")
        history = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual([item["phase"] for item in history], ["short_break", "focus"])
        focus = history[1]
        self.assertEqual(focus["taskTitle"], "Write CLI coverage")
        self.assertEqual(focus["taskContext"], "retained")
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
                "taskContext",
                "taskTitle",
            }.issubset(focus)
        )
        self.assertEqual(history[0]["taskContext"], "unassigned")
        self.assertEqual(history[0]["taskTitle"], "Unassigned")
        self.assertEqual(error, "")

        result, output, error = self.invoke("history", "--limit", "1", "--json")
        limited = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0]["phase"], "short_break")
        self.assertEqual(error, "")

    def test_cancelled_arrival_and_dismiss_command_use_terminal_semantics(self) -> None:
        self.assertEqual(self.invoke("start", "focus", "--minutes", "1")[0], 0)
        self.assertEqual(self.invoke("cancel")[0], 0)

        result, output, error = self.invoke("history")

        self.assertEqual((result, error), (0, ""))
        self.assertIn("Focus | Unassigned | Cancelled | 1 min", output)

        result, output, error = self.invoke("history", "--json")
        history = json.loads(output)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(history[0]["status"], "cancelled")
        self.assertEqual(history[0]["taskContext"], "unassigned")

        self.assertEqual(self.invoke("start", "focus", "--minutes", "1")[0], 0)
        self.assertEqual(self.invoke("finish")[0], 0)
        result, output, error = self.invoke("dismiss")
        self.assertEqual((result, error), (0, ""))
        self.assertIn("Short break | idle", output)

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
        self.store.set_selected_task_id(task["id"], now_ms=2)
        self.store.queue_duration_operation("focus", 2 * 60_000, now_ms=3)
        self.store.set_auto_start_breaks(True, now_ms=4)
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
        projection = self.store.projected_state(now_ms=1, state=stored_state)
        raw_path = "~/owned-state.sqlite3"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("pomodorough.cli.Store", autospec=True) as store_type:
            owned_store = store_type.return_value
            owned_store.load.return_value = stored_state
            owned_store.effective_timer_now_ms.return_value = 1
            owned_store.projected_state.return_value = projection
            owned_store.projected_settings.return_value = (
                self.store.projected_settings(stored_state, projection)
            )
            owned_store.projected_history.return_value = projection.history
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
