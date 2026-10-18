"""R50-D02: pending-break read failure must not block timer rendering."""

from __future__ import annotations

import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.controller_outcomes import EmitNotice, Render
from pomodorough.sentry_monitoring import (
    note_pending_break_read_recovery,
    reset_clock_incident_budget,
    reset_pending_break_read_incident_budget,
    reset_room_read_incident_budget,
    should_capture_clock_failure,
    should_capture_pending_break_read_failure,
    should_capture_room_read_failure,
)
from pomodorough.shared_core import SharedCoreError
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)

DURATIONS = {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}


def _running_timer() -> dict[str, object]:
    return {
        "id": "timer-r50-d02-running",
        "phase": "focus",
        "status": "running",
        "plannedDurationMs": DURATIONS["focus"],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "start"},
    }


def _idle_timer() -> dict[str, object]:
    return {
        "id": "timer-r50-d02-idle",
        "phase": "focus",
        "status": "idle",
        "plannedDurationMs": DURATIONS["focus"],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "clear"},
    }


def _harness(
    *,
    user: dict[str, object] | None = None,
    authenticated: bool = False,
    busy: bool = False,
    timer: dict[str, object] | None = None,
    history_active: bool = False,
) -> SimpleNamespace:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    cloud = SimpleNamespace(authenticated=authenticated, busy=busy)
    state = SimpleNamespace(timer=_running_timer() if timer is None else timer)
    settings = {
        "selectedPhase": "focus",
        "durationsMs": dict(DURATIONS),
        "selectedTaskId": None,
    }

    def context() -> TimerInteractionContext:
        return TimerInteractionContext(
            store, cloud, False, state.timer, settings, user,
            [], {}, 0, "offline", history_active,
        )

    ports = TimerInteractionPorts(
        context=context,
        apply_outcome=Mock(),
        mutation_blocked=Mock(return_value=False),
        issue_command=Mock(),
        maybe_auto_start_break=Mock(return_value=False),
        notice=Mock(),
        task_input_text=Mock(return_value=""),
        clear_task_input=Mock(),
        task_item_data=Mock(return_value=None),
        invalidate_task_selector=Mock(),
        render_task_selector=Mock(),
        refresh_duration_spins=Mock(),
        refresh_auto_breaks=Mock(),
        stop_sound_timer=Mock(),
        stop_completion_sound=Mock(),
        set_stop_sound_control=Mock(),
    )
    controller = TimerInteractionController(ports)
    return SimpleNamespace(store=store, controller=controller, ports=ports)


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


def _has_render(outcome) -> bool:
    return any(isinstance(e, Render) for e in outcome.effects)


class PendingBreakRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_pending_break_read_incident_budget()

    def tearDown(self) -> None:
        reset_pending_break_read_incident_budget()

    def test_twenty_failures_still_render_with_one_capture(self) -> None:
        harness = _harness()
        harness.store.has_pending_auto_break.side_effect = (
            sqlite3.OperationalError("not authorized")
        )
        renders = 0
        notices: list[str] = []
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            for _ in range(20):
                outcome = harness.controller.tick()
                renders += 1 if _has_render(outcome) else 0
                notices.extend(_notices(outcome))
        self.assertEqual(harness.store.has_pending_auto_break.call_count, 20)
        self.assertEqual(renders, 20)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(len(notices), 1)
        self.assertIn("not authorized", notices[0])
        harness.ports.maybe_auto_start_break.assert_not_called()

    def test_fail_closed_skips_auto_break_mutation(self) -> None:
        harness = _harness()
        harness.store.has_pending_auto_break.side_effect = OSError("down")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ):
            outcome = harness.controller.tick()
        self.assertTrue(_has_render(outcome))
        harness.ports.maybe_auto_start_break.assert_not_called()

    def test_recovery_allows_recurrence(self) -> None:
        harness = _harness()
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("bad")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            first = harness.controller.tick()
            self.assertEqual(len(_notices(first)), 1)
            self.assertEqual(capture.call_count, 1)
            second = harness.controller.tick()
            self.assertEqual(_notices(second), [])
            self.assertEqual(capture.call_count, 1)
        harness.store.has_pending_auto_break.side_effect = None
        harness.store.has_pending_auto_break.return_value = False
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            recovered = harness.controller.tick()
            self.assertEqual(_notices(recovered), [])
            self.assertTrue(_has_render(recovered))
            self.assertEqual(capture.call_count, 0)
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("bad")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            recurred = harness.controller.tick()
            self.assertEqual(len(_notices(recurred)), 1)
            self.assertEqual(capture.call_count, 1)

    def test_distinct_categories_each_report(self) -> None:
        harness = _harness()
        errors: list[BaseException] = [
            OSError("a"),
            sqlite3.OperationalError("b"),
            SharedCoreError("c"),
        ]
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            for error in errors:
                harness.store.has_pending_auto_break.side_effect = error
                outcome = harness.controller.tick()
                self.assertEqual(len(_notices(outcome)), 1)
            self.assertEqual(capture.call_count, 3)
            harness.store.has_pending_auto_break.side_effect = errors[-1]
            outcome = harness.controller.tick()
            self.assertEqual(_notices(outcome), [])
            self.assertEqual(capture.call_count, 3)

    def test_validation_notice_once_without_capture(self) -> None:
        harness = _harness()
        harness.store.has_pending_auto_break.side_effect = ValueError("bad row")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            first = harness.controller.tick()
            self.assertEqual(len(_notices(first)), 1)
            self.assertTrue(_has_render(first))
            second = harness.controller.tick()
            self.assertEqual(_notices(second), [])
            capture.assert_not_called()
        harness.store.has_pending_auto_break.side_effect = OSError("infra")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.tick()
            self.assertEqual(len(_notices(outcome)), 1)
            self.assertEqual(capture.call_count, 1)

    def test_idle_timer_has_no_render_but_bounded_notice(self) -> None:
        harness = _harness(timer=_idle_timer())
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("deny")
        renders = 0
        notices: list[str] = []
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            for _ in range(20):
                outcome = harness.controller.tick()
                renders += 1 if _has_render(outcome) else 0
                notices.extend(_notices(outcome))
        self.assertEqual(renders, 0)
        self.assertEqual(len(notices), 1)
        self.assertEqual(capture.call_count, 1)

    def test_history_active_still_renders_with_bounded_notice(self) -> None:
        harness = _harness(history_active=True)
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("deny")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ):
            first = harness.controller.tick()
            self.assertTrue(_has_render(first))
            self.assertEqual(len(_notices(first)), 1)
            second = harness.controller.tick()
            self.assertTrue(_has_render(second))
            self.assertEqual(_notices(second), [])

    def test_authenticated_and_busy_skip_pending_read(self) -> None:
        for kwargs in (
            {"user": {"id": "u"}, "authenticated": True, "busy": False},
            {"user": None, "authenticated": False, "busy": True},
        ):
            reset_pending_break_read_incident_budget()
            harness = _harness(**kwargs)
            harness.store.has_pending_auto_break.side_effect = sqlite3.Error(
                "boom"
            )
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.tick()
            harness.store.has_pending_auto_break.assert_not_called()
            capture.assert_not_called()
            self.assertEqual(_notices(outcome), [])
            self.assertTrue(_has_render(outcome))

    def test_schedule_path_shares_bounded_budget(self) -> None:
        harness = _harness()
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("deny")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            first = harness.controller.schedule_pending_auto_break()
            self.assertEqual(len(_notices(first)), 1)
            for _ in range(4):
                outcome = harness.controller.schedule_pending_auto_break()
                self.assertEqual(_notices(outcome), [])
            self.assertEqual(capture.call_count, 1)

    def test_budget_independent_of_clock_and_room(self) -> None:
        reset_clock_incident_budget()
        reset_room_read_incident_budget()
        self.assertTrue(should_capture_clock_failure(OSError("clock down")))
        self.assertTrue(should_capture_room_read_failure(OSError("room down")))
        self.assertTrue(
            should_capture_pending_break_read_failure(OSError("pending down"))
        )
        self.assertFalse(
            should_capture_pending_break_read_failure(OSError("pending again"))
        )
        note_pending_break_read_recovery()
        self.assertTrue(
            should_capture_pending_break_read_failure(OSError("pending again"))
        )
        self.assertFalse(should_capture_clock_failure(OSError("clock again")))
        self.assertFalse(should_capture_room_read_failure(OSError("room again")))


class RealSqliteDenialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _window_with_running_timer(self, tmp_path):
        import tempfile
        from pathlib import Path

        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QSystemTrayIcon
        from unittest.mock import patch

        from pomodorough.shared_core import SharedCore
        from pomodorough.storage import Store
        from pomodorough.ui import MainWindow
        from test_ui import FakeCloud, MemorySecretStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(
            Path(directory.name) / "state.sqlite3",
            shared_core=SharedCore(),
            iroh_secret_store=MemorySecretStore(),
        )
        self.addCleanup(store.close)
        cloud = FakeCloud(authenticated=False)
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            window = MainWindow(store, cloud, QIcon())
        window.tick_timer.stop()
        window.sync_timer.stop()
        self.addCleanup(window.close)
        with patch("pomodorough.ui.QMessageBox.warning"):
            window._issue("start")
        self.assertEqual(window._current_timer()["status"], "running")
        return window, store

    def test_real_denial_renders_and_sql_unchanged(self) -> None:
        import tempfile

        from unittest.mock import patch

        reset_pending_break_read_incident_budget()
        window, store = self._window_with_running_timer(tempfile.mkdtemp())
        before = list(store.connection.iterdump())
        reads = {"count": 0}

        def authorizer(action, arg1, arg2, db_name, trigger) -> int:
            if action == sqlite3.SQLITE_READ and arg1 == "pending_auto_breaks":
                reads["count"] += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.connection.set_authorizer(authorizer)
        window.notice.disconnect()
        notices: list[str] = []
        window.notice.connect(notices.append)
        renders = {"count": 0}
        original_render = window._render
        window._render = lambda *a, **k: renders.__setitem__(  # noqa: E731
            "count", renders["count"] + 1
        ) or original_render(*a, **k)
        try:
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                for _ in range(20):
                    window._tick()
                self.assertEqual(reads["count"], 20)
                self.assertEqual(renders["count"], 20)
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(len(notices), 1)
                self.assertIn("not authorized", notices[0].lower())
                store.connection.set_authorizer(None)
                self.assertEqual(list(store.connection.iterdump()), before)
                window._tick()
                self.assertEqual(renders["count"], 21)
                self.assertEqual(len(notices), 1)
                self.assertEqual(capture.call_count, 1)
        finally:
            store.connection.set_authorizer(None)
            window._render = original_render

    def test_modal_guard_keeps_single_dialog(self) -> None:
        import tempfile

        from unittest.mock import patch

        from PySide6.QtWidgets import QMessageBox

        reset_pending_break_read_incident_budget()
        window, _ = self._window_with_running_timer(tempfile.mkdtemp())
        self.assertFalse(window._notice_dialog_active)
        with patch.object(QMessageBox, "warning") as warning:
            window._show_notice("first")
            self.assertEqual(warning.call_count, 1)
            self.assertFalse(window._notice_dialog_active)
        with patch.object(
            QMessageBox, "warning", side_effect=lambda *a, **k: (
                window._show_notice("nested") or 0
            ),
        ) as warning:
            window._show_notice("outer")
            self.assertEqual(warning.call_count, 1)
            self.assertFalse(window._notice_dialog_active)


if __name__ == "__main__":
    unittest.main()
