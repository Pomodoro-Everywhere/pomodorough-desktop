from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from pomodorough.timer_screen import TimerScreen
from pomodorough.timer_view import ClockWidget
from pomodorough.ui_views import MainWindowViewMixin

NO_FIXED_MAX = 16777215


def _valid_settings() -> dict:
    return {
        "durations": {"focus": 25, "short_break": 5, "long_break": 15},
        "durationsMs": {
            "focus": 25 * 60_000,
            "short_break": 5 * 60_000,
            "long_break": 15 * 60_000,
        },
        "autoStartBreaks": False,
    }


def _history_item(index: int) -> dict:
    statuses = ("completed", "cancelled", "superseded")
    return {
        "id": f"history-{index}",
        "taskId": None,
        "phase": "focus",
        "status": statuses[index % 3],
        "plannedDurationMs": 1_500_000,
        "completedAt": "2026-01-01T12:00:00Z",
    }


class ArrivalsShowAllTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.strings = Strings("en")

    def test_retained_beyond_eight_all_render(self) -> None:
        screen = ArrivalsScreen(self.strings, "device-12345678")
        history = [_history_item(index) for index in range(12)]
        screen.render(history, {})
        self.assertEqual(screen.history_list.count(), 12)

    def test_header_counts_match_rendered_total(self) -> None:
        screen = ArrivalsScreen(self.strings, "device-12345678")
        history = [_history_item(index) for index in range(12)]
        screen.render(history, {})
        self.assertIn("12", screen.history_count.text())
        self.assertIn("12", screen.history_list.accessibleDescription())

    def test_list_scrolls_instead_of_truncating(self) -> None:
        screen = ArrivalsScreen(self.strings, "device-12345678")
        self.assertNotEqual(
            screen.history_list.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )


class TaskSelectorHeightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_no_fixed_maximum_height(self) -> None:
        screen = TimerScreen(Strings("en"), _valid_settings())
        self.assertEqual(screen.task_selector_panel.maximumHeight(), NO_FIXED_MAX)

    def test_focus_label_wraps(self) -> None:
        screen = TimerScreen(Strings("en"), _valid_settings())
        label = screen.task_selector_panel.findChild(QLabel)
        self.assertIsNotNone(label)
        assert label is not None
        self.assertTrue(label.wordWrap())


class InviteGrowableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fields_have_no_fixed_maximum(self) -> None:
        screen = NetworkScreen(Strings("en"), "iroh")
        self.assertEqual(screen.invite_input.maximumHeight(), NO_FIXED_MAX)
        self.assertEqual(screen.invite_output.maximumHeight(), NO_FIXED_MAX)

    def test_fields_expand_vertically(self) -> None:
        screen = NetworkScreen(Strings("en"), "iroh")
        for field in (screen.invite_input, screen.invite_output):
            with self.subTest(field=field.accessibleName()):
                self.assertEqual(
                    field.sizePolicy().verticalPolicy(),
                    QSizePolicy.Policy.Expanding,
                )

    def test_minimum_derives_from_font_metrics(self) -> None:
        screen = NetworkScreen(Strings("en"), "iroh")
        for field in (screen.invite_input, screen.invite_output):
            with self.subTest(field=field.accessibleName()):
                self.assertGreaterEqual(
                    field.minimumHeight(), field.fontMetrics().lineSpacing()
                )

    def test_long_ticket_round_trips(self) -> None:
        screen = NetworkScreen(Strings("en"), "iroh")
        ticket = "TICKET-" + "x" * 2000
        screen.invite_input.setPlainText(ticket)
        screen.invite_output.setPlainText(ticket)
        self.assertEqual(screen.invite_input.toPlainText(), ticket)
        self.assertEqual(screen.invite_output.toPlainText(), ticket)


class ClockFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_clock_is_keyboard_focusable(self) -> None:
        widget = ClockWidget(Strings("en"))
        self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_clock_keeps_accessible_identity(self) -> None:
        widget = ClockWidget(Strings("en"))
        self.assertTrue(widget.accessibleName())
        widget.set_state("25:00", "Focus", "Idle", 0.0)
        self.assertIn("25:00", widget.accessibleDescription())


class DisabledButtonPaletteTests(unittest.TestCase):
    def test_disabled_rule_exists_and_differs_from_enabled(self) -> None:
        sheet = MainWindowViewMixin._stylesheet()
        self.assertIn("QPushButton:disabled", sheet)
        enabled = [
            line
            for line in sheet.splitlines()
            if line.strip().startswith("QPushButton {")
        ]
        disabled = [
            line for line in sheet.splitlines() if "QPushButton:disabled" in line
        ]
        self.assertTrue(enabled)
        self.assertTrue(disabled)
        self.assertNotEqual(enabled[0], disabled[0])

    def test_disabled_uses_contrast_safe_roles(self) -> None:
        sheet = MainWindowViewMixin._stylesheet()
        disabled = "\n".join(line for line in sheet.splitlines() if ":disabled" in line)
        self.assertIn("color: palette(window-text)", disabled)
        self.assertIn("background: palette(midlight)", disabled)
        self.assertNotIn("color: palette(mid)", disabled)

    def test_primary_disabled_override_present(self) -> None:
        sheet = MainWindowViewMixin._stylesheet()
        self.assertIn("QPushButton#primaryButton:disabled", sheet)


if __name__ == "__main__":
    unittest.main()
