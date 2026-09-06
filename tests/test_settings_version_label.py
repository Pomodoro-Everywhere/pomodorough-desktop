from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from pomodorough import __version__
from pomodorough import timer_screen
from pomodorough.localization import Strings
from pomodorough.timer_screen import TimerScreen


def _settings() -> dict:
    return {
        "durations": {"focus": 25, "short_break": 5, "long_break": 15},
        "autoStartBreaks": False,
    }


class SettingsVersionLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_panel_shows_running_version(self) -> None:
        strings = Strings("en")
        screen = TimerScreen(strings, _settings())
        expected = strings.text("pattern.version", version=__version__)
        self.assertEqual(screen.version_label.text(), expected)
        self.assertIn(__version__, screen.version_label.text())
        self.assertIn(screen.version_label.objectName(), ("taskSubtitle", "privacyNotice"))
        labels = screen.right_panel.findChildren(QLabel)
        self.assertIn(screen.version_label, labels)

    def test_version_label_updates_with_version(self) -> None:
        strings = Strings("en")
        updated = f"{__version__}-next"
        with patch.object(timer_screen, "__version__", updated):
            screen = TimerScreen(strings, _settings())
            expected = strings.text("pattern.version", version=updated)
            self.assertEqual(screen.version_label.text(), expected)
            self.assertIn(updated, screen.version_label.text())


if __name__ == "__main__":
    unittest.main()
