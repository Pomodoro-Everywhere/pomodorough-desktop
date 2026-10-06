"""R43-D07: timer-tick renders must not reset arrival row or viewport."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.localization import Strings


def _history_item(index: int) -> dict:
    statuses = ("completed", "cancelled", "superseded")
    return {
        "id": f"history-{index}",
        "timerId": f"timer-{index}",
        "taskId": None,
        "phase": "focus",
        "status": statuses[index % 3],
        "plannedDurationMs": 1_500_000,
        "completedAt": "2026-01-01T12:00:00Z",
    }


def _make_screen() -> ArrivalsScreen:
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.resize(300, 120)
    screen.show()
    QApplication.processEvents()
    return screen


class ArrivalStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_repeated_ticks_preserve_current_row(self) -> None:
        screen = _make_screen()
        history = [_history_item(index) for index in range(50)]
        screen.render(history, {})
        screen.history_list.setCurrentRow(30)
        for _ in range(5):
            screen.render(history, {})
        self.assertEqual(screen.history_list.currentRow(), 30)

    def test_repeated_ticks_preserve_viewport(self) -> None:
        screen = _make_screen()
        history = [_history_item(index) for index in range(50)]
        screen.render(history, {})
        screen.history_list.setCurrentRow(30)
        QApplication.processEvents()
        bar = screen.history_list.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        expected = bar.value()
        for _ in range(5):
            screen.render(history, {})
        QApplication.processEvents()
        self.assertEqual(screen.history_list.currentRow(), 30)
        self.assertEqual(bar.value(), expected)

    def test_new_arrival_insertion_preserves_current_identity(self) -> None:
        screen = _make_screen()
        history = [_history_item(index) for index in range(50)]
        screen.render(history, {})
        screen.history_list.setCurrentRow(30)
        current_id = screen.history_list.currentItem().data(
            Qt.ItemDataRole.UserRole
        )
        self.assertEqual(current_id, "history-30")
        extended = list(history) + [_history_item(50)]
        screen.render(extended, {})
        self.assertEqual(screen.history_list.count(), 51)
        current = screen.history_list.currentItem()
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.data(Qt.ItemDataRole.UserRole), "history-30")
        self.assertEqual(screen.history_list.currentRow(), 30)


if __name__ == "__main__":
    unittest.main()
