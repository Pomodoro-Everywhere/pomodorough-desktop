from __future__ import annotations

import unittest

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.network_screen import NetworkScreen
from pomodorough.tasks_screen import TasksScreen
from pomodorough.timer_screen import TimerScreen
from pomodorough.ui_views import MainWindowViewMixin


class StylesheetTextContrastTests(unittest.TestCase):
    def test_no_label_uses_mid_as_text_color(self) -> None:
        combined = "\n".join(
            (
                MainWindowViewMixin._stylesheet(),
                TimerScreen.stylesheet(),
                TasksScreen.stylesheet(),
                ArrivalsScreen.stylesheet(),
                NetworkScreen.stylesheet(),
            )
        )
        self.assertNotIn("color: palette(mid)", combined)

    def test_secondary_labels_use_text_role(self) -> None:
        self.assertIn(
            "QLabel#taskSubtitle { color: palette(text)",
            MainWindowViewMixin._stylesheet(),
        )
        self.assertIn(
            "QLabel#emptyState { color: palette(text)",
            TasksScreen.stylesheet(),
        )
        self.assertIn(
            "QLabel#privacyNotice { color: palette(text)",
            NetworkScreen.stylesheet(),
        )


if __name__ == "__main__":
    unittest.main()
