from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QVBoxLayout

from pomodorough import app as app_module
from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen
from pomodorough.timer_screen import TimerScreen
from pomodorough.ui_views import MainWindowViewMixin

FOCUS_MS = 25 * 60_000


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


class TimerDurationFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.strings = Strings("en")

    def test_build_tolerates_missing_durations(self) -> None:
        screen = TimerScreen(self.strings, {})
        self.assertEqual(screen.duration_spins["focus"].value(), 25)
        self.assertEqual(screen.duration_spins["short_break"].value(), 5)
        self.assertEqual(screen.duration_spins["long_break"].value(), 15)

    def test_build_tolerates_corrupt_duration_values(self) -> None:
        settings = {
            "durations": {
                "focus": "bogus",
                "short_break": True,
                "long_break": 999,
            },
            "autoStartBreaks": False,
        }
        screen = TimerScreen(self.strings, settings)
        self.assertEqual(screen.duration_spins["focus"].value(), 25)
        self.assertEqual(screen.duration_spins["short_break"].value(), 5)
        self.assertEqual(screen.duration_spins["long_break"].value(), 15)

    def test_refresh_tolerates_corrupt_settings(self) -> None:
        screen = TimerScreen(self.strings, _valid_settings())
        screen.refresh_duration_spins({"durations": {"focus": None}})
        self.assertEqual(screen.duration_spins["focus"].value(), 25)
        self.assertEqual(screen.duration_spins["short_break"].value(), 5)

    def test_presentation_tolerates_missing_durations_ms(self) -> None:
        state = TimerScreen.presentation(
            {"status": "idle", "phase": "focus"},
            selected_phase="focus",
            settings={},
            now_ms=1_000,
        )
        self.assertEqual(state.planned, FOCUS_MS)
        self.assertEqual(state.remaining, FOCUS_MS - state.elapsed)

    def test_presentation_tolerates_corrupt_planned_duration(self) -> None:
        for planned in (None, "bogus", True, -5, 0):
            with self.subTest(planned=planned):
                state = TimerScreen.presentation(
                    {
                        "status": "idle",
                        "phase": "focus",
                        "plannedDurationMs": planned,
                    },
                    selected_phase="focus",
                    settings=_valid_settings(),
                    now_ms=1_000,
                )
                self.assertEqual(state.planned, FOCUS_MS)

    def test_presentation_tolerates_unknown_phase(self) -> None:
        state = TimerScreen.presentation(
            {"status": "idle", "phase": "focus"},
            selected_phase="bogus",
            settings=_valid_settings(),
            now_ms=1_000,
        )
        self.assertEqual(state.planned, FOCUS_MS)


class TasksMissingSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.strings = Strings("en")

    def test_render_skips_missing_summary_with_placeholder(self) -> None:
        screen = TasksScreen(self.strings)
        tasks = [
            {"id": 123, "title": "Numeric id"},
            {"title": "No id"},
        ]
        screen.render(tasks, [], mutations_enabled=True)  # type: ignore[list-item]
        self.assertEqual(screen.task_table.rowCount(), 2)
        self.assertEqual(screen.task_table.item(0, 0).text(), "Numeric id")
        self.assertEqual(screen.task_table.item(0, 1).text(), "0")
        self.assertEqual(screen.task_table.item(1, 0).text(), "No id")

    def test_render_totals_tolerate_corrupt_summaries(self) -> None:
        screen = TasksScreen(self.strings)
        screen._render_totals(
            {"task-1": {"finished": "bogus", "timeMs": None}}  # type: ignore[dict-item]
        )
        self.assertTrue(screen.task_totals.text())


class SettingsWidthHintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _harness(self) -> MainWindowViewMixin:
        harness = MainWindowViewMixin()
        harness.timer_screen = TimerScreen(Strings("en"), _valid_settings())  # type: ignore[attr-defined]
        harness.outer_layout = QVBoxLayout()  # type: ignore[attr-defined]
        return harness

    def test_hidden_settings_keep_base_minimum(self) -> None:
        harness = self._harness()
        self.assertEqual(harness._settings_minimum_width(False), 600)

    def test_visible_settings_cap_to_small_screen(self) -> None:
        harness = self._harness()
        with (
            patch.object(
                MainWindowViewMixin, "_settings_content_width_hint", return_value=2000
            ),
            patch.object(
                MainWindowViewMixin, "_available_screen_width", return_value=700
            ),
        ):
            self.assertEqual(harness._settings_minimum_width(True), 700)

    def test_visible_settings_follow_hint_without_screen_info(self) -> None:
        harness = self._harness()
        with (
            patch.object(
                MainWindowViewMixin, "_settings_content_width_hint", return_value=2000
            ),
            patch.object(
                MainWindowViewMixin, "_available_screen_width", return_value=0
            ),
        ):
            self.assertEqual(harness._settings_minimum_width(True), 2000)

    def test_content_hint_derives_from_layout(self) -> None:
        harness = self._harness()
        hint = harness._settings_content_width_hint()
        self.assertGreater(hint, 0)


class SecondInstanceMessageTests(unittest.TestCase):
    def test_report_returns_nonzero_and_prints_message(self) -> None:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            result = app_module._report_second_instance()
        self.assertEqual(result, app_module.SECOND_INSTANCE_EXIT_CODE)
        self.assertNotEqual(result, 0)
        self.assertIn(app_module.SECOND_INSTANCE_MESSAGE, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
