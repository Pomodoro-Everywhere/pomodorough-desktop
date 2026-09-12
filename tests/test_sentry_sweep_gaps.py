"""Sentry-gap sweep residuals: CLI capture split, schedule_pending guard."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough.cli import main
from pomodorough.controller_outcomes import EmitNotice
from pomodorough.storage import Store
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


class CliCaptureSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _invoke_owned(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = main(
            arguments, store=self.store, stdout=stdout, stderr=stderr,
        )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_sqlite_error_captures_and_stays_storage_error(self) -> None:
        with patch.object(
            self.store, "load",
            side_effect=sqlite3.OperationalError("disk down"),
        ):
            with patch("pomodorough.cli.capture_exception") as capture:
                result, output, error = self._invoke_owned("status", "--json")
        capture.assert_called_once()
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        payload = json.loads(error)
        self.assertEqual(payload["code"], "storage_error")
        self.assertEqual(payload["type"], "StorageError")

    def test_json_decode_stays_silent(self) -> None:
        corrupt = json.JSONDecodeError("bad persisted JSON", "{", 1)
        with patch.object(self.store, "load", side_effect=corrupt):
            with patch("pomodorough.cli.capture_exception") as capture:
                result, _output, error = self._invoke_owned("status", "--json")
        capture.assert_not_called()
        self.assertEqual(result, 2)
        self.assertIn("bad persisted JSON", error)

    def test_close_failure_captures(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("pomodorough.cli.Store", return_value=self.store):
            with patch.object(
                self.store, "close", side_effect=OSError("close failed"),
            ):
                with patch("pomodorough.cli.capture_exception") as capture:
                    result = main(
                        ("status", "--json"),
                        stdout=stdout, stderr=stderr,
                    )
        capture.assert_called_once()
        self.assertEqual(result, 2)


def _timer_harness() -> SimpleNamespace:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    cloud = SimpleNamespace(authenticated=False, busy=False)
    ports = TimerInteractionPorts(
        context=lambda: TimerInteractionContext(
            store, cloud, False, None, {}, None, [], {}, 0,
            "offline", False,
        ),
        apply_outcome=Mock(), mutation_blocked=Mock(return_value=False),
        issue_command=Mock(), maybe_auto_start_break=Mock(),
        notice=Mock(), task_input_text=Mock(return_value=""),
        clear_task_input=Mock(), task_item_data=Mock(return_value=None),
        invalidate_task_selector=Mock(), render_task_selector=Mock(),
        refresh_duration_spins=Mock(), refresh_auto_breaks=Mock(),
        stop_sound_timer=Mock(), stop_completion_sound=Mock(),
        set_stop_sound_control=Mock(),
    )
    return SimpleNamespace(
        store=store, controller=TimerInteractionController(ports),
    )


class SchedulePendingGuardTests(unittest.TestCase):
    def test_infra_captures_with_notice(self) -> None:
        harness = _timer_harness()
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("db")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.schedule_pending_auto_break()
        capture.assert_called_once()
        self.assertTrue(_notices(outcome))

    def test_validation_stays_silent_with_notice(self) -> None:
        harness = _timer_harness()
        harness.store.has_pending_auto_break.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.schedule_pending_auto_break()
        capture.assert_not_called()
        self.assertTrue(_notices(outcome))

    def test_no_pending_schedules_nothing(self) -> None:
        harness = _timer_harness()
        with patch(
            "pomodorough.timer_interaction_controller.QTimer",
        ) as timer:
            outcome = harness.controller.schedule_pending_auto_break()
        timer.singleShot.assert_not_called()
        self.assertEqual(_notices(outcome), [])

    def test_pending_schedules_singleshot(self) -> None:
        harness = _timer_harness()
        harness.store.has_pending_auto_break.return_value = True
        with patch(
            "pomodorough.timer_interaction_controller.QTimer",
        ) as timer:
            outcome = harness.controller.schedule_pending_auto_break()
        timer.singleShot.assert_called_once()
        self.assertEqual(_notices(outcome), [])


class UiViewsSilenceCommentTests(unittest.TestCase):
    def test_cached_room_validation_comment_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "pomodorough"
        text = (root / "ui_views.py").read_text(encoding="utf-8")
        self.assertIn("# validation stays silent", text)


if __name__ == "__main__":
    unittest.main()
