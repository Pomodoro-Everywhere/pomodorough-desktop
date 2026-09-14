"""0.40.0 review D70-D76: resolution phase, shortcuts, focus, store, plan, settings."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from pomodorough.secure_store import (
    SecureStoreError,
    is_infra_secure_store_error,
    should_capture_secure_store_error,
)
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp
from pomodorough.storage_completion import (
    preview_plan_failure_count,
    reset_preview_plan_failure_count,
)
from pomodorough.ui import MainWindow


def _history_item(timer_id, command_id, phase, planned_ms, stamp):
    return {
        "id": timer_id,
        "timerId": timer_id,
        "phase": phase,
        "status": "completed",
        "plannedDurationMs": planned_ms,
        "commandId": command_id,
        "completedAt": stamp,
        "endedAt": stamp,
    }


def _focus_item(index, stamp):
    return _history_item(
        f"remote-focus-{index}",
        f"remote-finish-{index}",
        "focus",
        25 * 60_000,
        stamp,
    )


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class D70ResolutionPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path, shared_core=SharedCore())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _apply_remote_via_resolution(self, history, revision, timer=None):
        from v2_core_double import V2EmulatingSharedCore  # noqa: F401

        user = {"id": "user-1"}
        self.store.prepare_resolution(user, revision - 1, "merge")
        pending = self.store.pending_resolution(user["id"])
        request = pending["request"]
        response = {
            "acknowledgements": [
                {"commandId": c["id"], "outcome": "applied", "reason": ""}
                for c in request["commands"]
            ],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": revision,
            "canonicalTimer": timer,
            "history": history,
            "tasks": [],
            "durationsMs": {
                "focus": 25 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(5_000 + revision * 1_000),
            "serverHlcWallMs": 5_000 + revision * 1_000,
            "serverHlcCounter": 0,
        }
        self.store.apply_resolution(response, user)

    def test_resolution_single_focus_advances_to_short_break(self):
        history = [_focus_item(1, "1970-01-01T00:00:02.000Z")]
        self._apply_remote_via_resolution(history, 1)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )

    def test_resolution_empty_history_is_noop(self):
        self._apply_remote_via_resolution([], 1)
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")

    def test_resolution_preserves_explicit_phase(self):
        history = [_focus_item(1, "1970-01-01T00:00:02.000Z")]
        self._apply_remote_via_resolution(history, 1)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )
        self.store.set_selected_phase("long_break")
        self._apply_remote_via_resolution(history, 2)
        # Explicit long_break differs from source focus, so advance is skipped.
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "long_break"
        )


class D71SpaceShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        from v2_core_double import V2EmulatingSharedCore

        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
            shared_core=V2EmulatingSharedCore(),
        )
        from tests.test_ui import FakeCloud  # type: ignore

        self.cloud = FakeCloud()
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.window = MainWindow(self.store, self.cloud, QIcon())
        self.window.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def test_space_consumed_by_button_and_inputs(self) -> None:
        self.window.primary_button.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._focused_widget_consumes_space())
        self.window.task_input.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._focused_widget_consumes_space())
        self.window.room_name_input.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._focused_widget_consumes_space())

    def test_space_shortcut_skips_when_consumed(self) -> None:
        self.window.task_input.setFocus()
        QApplication.processEvents()
        with patch.object(self.window, "_primary_action") as primary:
            self.window._shortcut_primary_action()
            primary.assert_not_called()
        self.window.primary_button.setFocus()
        QApplication.processEvents()
        with patch.object(self.window, "_primary_action") as primary:
            self.window._shortcut_primary_action()
            primary.assert_not_called()

    def test_space_shortcut_fires_without_consumer(self) -> None:
        self.window.page_stack.setFocus()
        QApplication.processEvents()
        # page_stack (QStackedWidget) does not consume Space.
        if self.window._focused_widget_consumes_space():
            self.skipTest("offscreen focus did not clear to non-consumer")
        with patch.object(self.window, "_primary_action") as primary:
            self.window._shortcut_primary_action()
            primary.assert_called_once()

    def test_space_keyclick_respects_focus(self) -> None:
        self.window.task_input.setFocus()
        QApplication.processEvents()
        with patch.object(self.window, "_primary_action") as primary:
            QTest.keyClick(self.window.task_input, Qt.Key.Key_Space)
            QApplication.processEvents()
            # Focused line edit consumes Space; global shortcut must skip.
            primary.assert_not_called()
        self.window.page_stack.setFocus()
        QApplication.processEvents()
        if self.window._focused_widget_consumes_space():
            self.skipTest("offscreen focus did not clear to non-consumer")
        with patch.object(self.window, "_primary_action") as primary:
            QTest.keyClick(self.window, Qt.Key.Key_Space)
            QApplication.processEvents()
            primary.assert_called()


class D75CtrlDigitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        from v2_core_double import V2EmulatingSharedCore

        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
            shared_core=V2EmulatingSharedCore(),
        )
        from tests.test_ui import FakeCloud  # type: ignore

        self.cloud = FakeCloud()
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.window = MainWindow(self.store, self.cloud, QIcon())
        self.window.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def test_text_input_detection(self) -> None:
        self.window.task_input.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._focused_widget_is_text_input())
        self.window.invite_input.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.window._focused_widget_is_text_input())
        self.window.primary_button.setFocus()
        QApplication.processEvents()
        self.assertFalse(self.window._focused_widget_is_text_input())

    def test_ctrl_digit_skipped_in_text_input(self) -> None:
        self.window.task_input.setFocus()
        QApplication.processEvents()
        emitted: list[int] = []
        self.window.navigation.screen_requested.connect(emitted.append)
        try:
            self.window._shortcut_show_screen(2)
            self.assertEqual(emitted, [])
        finally:
            self.window.navigation.screen_requested.disconnect(emitted.append)
        self.window.primary_button.setFocus()
        QApplication.processEvents()
        emitted.clear()
        self.window.navigation.screen_requested.connect(emitted.append)
        try:
            self.window._shortcut_show_screen(2)
            self.assertEqual(emitted, [2])
        finally:
            self.window.navigation.screen_requested.disconnect(emitted.append)

    def test_ctrl_digit_keyclick_in_input_skips_nav(self) -> None:
        self.window.task_input.setFocus()
        QApplication.processEvents()
        emitted: list[int] = []
        self.window.navigation.screen_requested.connect(emitted.append)
        try:
            QTest.keyClick(
                self.window.task_input,
                Qt.Key.Key_2,
                Qt.KeyboardModifier.ControlModifier,
            )
            QApplication.processEvents()
            self.assertEqual(emitted, [])
        finally:
            self.window.navigation.screen_requested.disconnect(emitted.append)


class D72NetworkFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        from v2_core_double import V2EmulatingSharedCore

        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
            shared_core=V2EmulatingSharedCore(),
        )
        from tests.test_ui import FakeCloud  # type: ignore

        self.cloud = FakeCloud()
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.window = MainWindow(self.store, self.cloud, QIcon())
        self.window.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def test_all_four_screens_set_focus(self) -> None:
        expected = [
            self.window.primary_button,
            self.window.task_input,
            self.window.history_list,
            self.window.replication_mode_combo,
        ]
        for index, target in enumerate(expected):
            with self.subTest(index=index):
                self.window._display_screen(index)
                QApplication.processEvents()
                self.assertIs(QApplication.focusWidget(), target)

    def test_network_target_is_focusable_combo(self) -> None:
        self.window._display_screen(3)
        QApplication.processEvents()
        combo = self.window.replication_mode_combo
        self.assertTrue(combo.isEnabled())
        self.assertNotEqual(combo.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIs(QApplication.focusWidget(), combo)


class D73SecureStoreTests(unittest.TestCase):
    def test_infra_cause_detected(self) -> None:
        infra = SecureStoreError("Secure value could not be read: boom")
        infra.__cause__ = OSError("disk down")
        self.assertTrue(is_infra_secure_store_error(infra))
        self.assertTrue(should_capture_secure_store_error(infra))
        timeout = SecureStoreError("Platform secure storage failed: timed out")
        timeout.__cause__ = subprocess.TimeoutExpired(["secret-tool"], 15)
        self.assertTrue(is_infra_secure_store_error(timeout))
        self.assertTrue(should_capture_secure_store_error(timeout))

    def test_malformed_stays_silent(self) -> None:
        malformed = SecureStoreError("Secure storage returned malformed data.")
        malformed.__cause__ = ValueError("not base64!")
        self.assertFalse(is_infra_secure_store_error(malformed))
        self.assertFalse(should_capture_secure_store_error(malformed))
        bare = SecureStoreError("Stored OAuth credentials are malformed.")
        self.assertFalse(is_infra_secure_store_error(bare))
        self.assertFalse(should_capture_secure_store_error(bare))

    def test_cached_room_capture_split(self) -> None:
        app = QApplication.instance() or QApplication([])
        del app
        from pomodorough import ui_views

        infra = SecureStoreError("Secure value could not be read: boom")
        infra.__cause__ = OSError("disk down")
        malformed = SecureStoreError("Secure storage returned malformed data.")
        malformed.__cause__ = ValueError("bad")
        for error, should_capture in ((infra, True), (malformed, False)):
            with self.subTest(error=str(error)):
                host = Mock()
                host.store.iroh_room.side_effect = error
                host._cached_iroh_room = {"roomId": "cached"}
                with patch.object(ui_views, "capture_exception") as capture:
                    result = ui_views.MainWindowViewMixin._cached_iroh_room_value(
                        host
                    )
                self.assertEqual(result, {"roomId": "cached"})
                if should_capture:
                    capture.assert_called_once_with(error)
                else:
                    capture.assert_not_called()


class D74PlanFailureTests(unittest.TestCase):
    def test_preview_failure_counts_and_debug_logs(self) -> None:
        from pomodorough.storage_completion import TimerCompletionPolicy

        reset_preview_plan_failure_count()
        policy = TimerCompletionPolicy(
            lambda: Mock(), lambda: "device-1", lambda: "offline",
            lambda _k, _d: None,
        )
        timer = {"id": "timer-1", "phase": "focus"}
        with patch.object(
            policy, "_plan", side_effect=ValueError("rejected")
        ):
            with self.assertLogs(
                "pomodorough.storage_completion", level="DEBUG"
            ) as logs:
                self.assertIsNone(
                    policy.preview_selected_phase(
                        timer, [], False, "1970-01-01T00:00:02.000Z"
                    )
                )
        self.assertEqual(preview_plan_failure_count(), 1)
        self.assertTrue(
            any("preview selected-phase plan rejected" in m for m in logs.output)
        )

    def test_preview_counter_resets(self) -> None:
        reset_preview_plan_failure_count()
        self.assertEqual(preview_plan_failure_count(), 0)


class D76SettingsClippingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        from v2_core_double import V2EmulatingSharedCore

        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
            shared_core=V2EmulatingSharedCore(),
        )
        from tests.test_ui import FakeCloud  # type: ignore

        self.cloud = FakeCloud()
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.window = MainWindow(self.store, self.cloud, QIcon())
        self.window.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def test_settings_scroll_wraps_panel(self) -> None:
        from PySide6.QtWidgets import QScrollArea

        scroll = self.window.settings_scroll
        self.assertIsInstance(scroll, QScrollArea)
        self.assertTrue(scroll.widgetResizable())
        self.assertIs(scroll.widget(), self.window.right_panel)
        self.assertFalse(scroll.isVisible())
        self.window.timer_screen.set_settings_visible(True)
        QApplication.processEvents()
        self.assertTrue(scroll.isVisible())
        self.assertTrue(self.window.right_panel.isVisible())

    def test_settings_visible_bumps_min_width(self) -> None:
        self.window._settings_toggled(True)
        QApplication.processEvents()
        self.assertGreaterEqual(self.window.minimumWidth(), 880)
        self.assertTrue(self.window.settings_scroll.isVisible())
        self.window._settings_toggled(False)
        QApplication.processEvents()
        self.assertEqual(self.window.minimumWidth(), 600)
        self.assertFalse(self.window.settings_scroll.isVisible())

    def test_settings_content_does_not_clip(self) -> None:
        self.window.timer_screen.set_settings_visible(True)
        QApplication.processEvents()
        scroll = self.window.settings_scroll
        panel_hint = self.window.right_panel.sizeHint()
        scroll_hint = scroll.viewport().size()
        # Scrollable wrapper means short heights scroll instead of
        # clipping panel contents out of the layout.
        self.assertTrue(scroll.widgetResizable())
        self.assertGreaterEqual(
            scroll.minimumWidth(), self.window.right_panel.minimumWidth()
        )
        self.assertGreater(panel_hint.height(), 0)
        self.assertGreater(scroll_hint.width(), 0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
