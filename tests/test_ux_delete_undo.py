"""Tasks screen: Delete/Backspace remove the targeted row behind an undo banner."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QSystemTrayIcon

from pomodorough.localization import Strings
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.tasks_screen import TasksScreen
from pomodorough.ui import MainWindow


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _wait_until(condition, ticks: int = 200) -> None:
    # Spin the loop with processEvents like the rest of the suite. QTest.qWait
    # also flushes every deleteLater() queued by earlier test files, which
    # segfaults the full run; plain processEvents delivers timers without that.
    while ticks > 0 and not condition():
        QApplication.processEvents()
        time.sleep(0.005)
        ticks -= 1


def _tasks(count: int = 3) -> list[dict]:
    return [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(count)]


def _settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def screen():
    app = QApplication.instance() or QApplication([])
    view = TasksScreen(Strings("en"))
    view.resize(480, 360)
    view.show()
    view.activateWindow()
    _settle()
    view.render(_tasks(), [], mutations_enabled=True)
    _settle()
    deleted: list[str] = []
    view.delete_task_requested.connect(deleted.append)
    view.deleted = deleted
    yield view
    view.close()
    _settle()
    view.deleteLater()
    _settle()
    del app


def _focus(widget) -> None:
    widget.setFocus()
    _settle()
    assert QApplication.focusWidget() is widget


def _titles(view: TasksScreen) -> list[str]:
    table = view.task_table
    return [table.item(row, 0).text() for row in range(table.rowCount())]


def test_table_exposes_keyboard_target_as_row_selection(screen):
    table = screen.task_table
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    _focus(table)
    table.setCurrentCell(0, 0)
    QTest.keyClick(table, Qt.Key.Key_Down)
    _settle()
    assert table.currentRow() == 1
    assert sorted({index.row() for index in table.selectedIndexes()}) == [1]


@pytest.mark.parametrize("key", [Qt.Key.Key_Delete, Qt.Key.Key_Backspace])
def test_key_on_focused_delete_button_defers_that_row(screen, key):
    button = screen.task_table.cellWidget(1, 3)
    _focus(button)
    QTest.keyClick(button, key)
    _settle()
    assert screen.pending_delete_task_id() == "task-1"
    assert screen.deleted == []
    assert _titles(screen) == ["Task 0", "Task 2"]
    assert screen.undo_banner.isVisible()
    assert screen.undo_label.text() == "Deleted “Task 1”"
    assert screen.undo_label.accessibleName() == "Deleted “Task 1”"
    assert screen.undo_button.accessibleName() == "Undo deleting Task 1"
    assert screen.undo_button.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert screen.undo_dismiss_button.focusPolicy() & Qt.FocusPolicy.TabFocus


@pytest.mark.parametrize("key", [Qt.Key.Key_Delete, Qt.Key.Key_Backspace])
def test_key_on_current_row_defers_that_row(screen, key):
    table = screen.task_table
    _focus(table)
    table.setCurrentCell(2, 0)
    QTest.keyClick(table, key)
    _settle()
    assert screen.pending_delete_task_id() == "task-2"
    assert screen.deleted == []
    assert _titles(screen) == ["Task 0", "Task 1"]


def test_key_on_viewport_without_current_row_does_nothing(screen):
    table = screen.task_table
    _focus(table)
    table.setCurrentCell(-1, -1)
    QTest.keyClick(table.viewport(), Qt.Key.Key_Delete)
    _settle()
    assert screen.pending_delete_task_id() is None
    assert _titles(screen) == ["Task 0", "Task 1", "Task 2"]


def test_delete_key_in_task_input_edits_text_only(screen):
    screen.task_input.setText("abc")
    _focus(screen.task_input)
    screen.task_input.setCursorPosition(1)
    QTest.keyClick(screen.task_input, Qt.Key.Key_Delete)
    QTest.keyClick(screen.task_input, Qt.Key.Key_Backspace)
    _settle()
    assert screen.task_input.text() == "c"
    assert screen.pending_delete_task_id() is None
    assert screen.deleted == []
    assert _titles(screen) == ["Task 0", "Task 1", "Task 2"]


def test_disabled_rows_ignore_delete_keys(screen):
    screen.render(_tasks(), [], mutations_enabled=False)
    _settle()
    table = screen.task_table
    _focus(table)
    table.setCurrentCell(0, 0)
    QTest.keyClick(table, Qt.Key.Key_Delete)
    _settle()
    assert screen.pending_delete_task_id() is None
    assert screen.deleted == []


def test_dismiss_requests_delete_exactly_once(screen):
    screen.task_table.cellWidget(0, 3).click()
    assert screen.deleted == []
    screen.undo_dismiss_button.click()
    _settle()
    screen.flush_pending_delete()
    assert screen.deleted == ["task-0"]
    assert screen.pending_delete_task_id() is None
    assert not screen.undo_banner.isVisible()
    _wait_until(lambda: False, ticks=2)
    _settle()
    screen.flush_pending_delete()
    screen.flush_pending_delete()
    assert screen.deleted == ["task-0"]


def test_undo_restores_row_and_focus_without_request(screen):
    button = screen.task_table.cellWidget(1, 3)
    _focus(button)
    QTest.keyClick(button, Qt.Key.Key_Delete)
    _settle()
    assert _titles(screen) == ["Task 0", "Task 2"]
    _focus(screen.undo_button)
    screen.undo_button.click()
    _settle()
    assert screen.pending_delete_task_id() is None
    assert not screen.undo_banner.isVisible()
    assert _titles(screen) == ["Task 0", "Task 1", "Task 2"]
    focused = QApplication.focusWidget()
    assert focused is screen.task_table.cellWidget(1, 3)
    assert focused.property("taskId") == "task-1"
    assert screen.deleted == []
    screen.flush_pending_delete()
    assert screen.deleted == []


def test_second_delete_flushes_first_then_defers_new(screen):
    screen.task_table.cellWidget(0, 3).click()
    _settle()
    assert screen.pending_delete_task_id() == "task-0"
    row = next(
        row
        for row in range(screen.task_table.rowCount())
        if screen.task_table.cellWidget(row, 3).property("taskId") == "task-2"
    )
    screen.task_table.cellWidget(row, 3).click()
    _settle()
    assert screen.deleted == ["task-0"]
    assert screen.pending_delete_task_id() == "task-2"
    assert screen.undo_label.text() == "Deleted “Task 2”"
    assert "Task 2" not in _titles(screen)
    screen.flush_pending_delete()
    assert screen.deleted == ["task-0", "task-2"]
    screen.flush_pending_delete()
    assert screen.deleted == ["task-0", "task-2"]


def test_add_request_flushes_pending_delete_first(screen):
    added: list[str] = []
    screen.add_task_requested.connect(added.append)
    order: list[str] = []
    screen.delete_task_requested.connect(lambda _id: order.append("delete"))
    screen.add_task_requested.connect(lambda _title: order.append("add"))
    screen.task_table.cellWidget(0, 3).click()
    screen.task_input.setText("New task")
    screen.add_task_button.click()
    assert screen.deleted == ["task-0"]
    assert added == ["New task"]
    assert order == ["delete", "add"]


def test_render_keeps_pending_row_hidden_until_resolved(screen):
    screen.task_table.cellWidget(1, 3).click()
    _settle()
    screen.render(_tasks(4), [], mutations_enabled=True)
    _settle()
    assert _titles(screen) == ["Task 0", "Task 2", "Task 3"]
    screen.undo_pending_delete()
    _settle()
    assert _titles(screen) == ["Task 0", "Task 1", "Task 2", "Task 3"]


class _MainWindowHarness:
    def __enter__(self):
        self.app = QApplication.instance() or QApplication([])
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
            shared_core=SharedCore(),
        )
        from tests.test_ui import FakeCloud  # type: ignore

        self.cloud = FakeCloud()
        with patch.object(
            QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.window = MainWindow(self.store, self.cloud, QIcon())
        self.window.show()
        _settle()
        return self

    def __exit__(self, *_exc) -> None:
        self.window.quitting = True
        self.window.close()
        _settle()
        self.store.close()
        self.temporary.cleanup()


def test_main_window_shutdown_flushes_pending_delete_once():
    with _MainWindowHarness() as harness:
        window = harness.window
        window._add_task("Shutdown task")
        _settle()
        requested: list[str] = []
        window.tasks_screen.delete_task_requested.connect(requested.append)
        table = window.tasks_screen.task_table
        assert table.rowCount() == 1
        task_id = table.cellWidget(0, 3).property("taskId")
        table.cellWidget(0, 3).click()
        _settle()
        assert window.tasks_screen.pending_delete_task_id() == task_id
        assert requested == []
        window.shutdown()
        assert requested == [task_id]
        assert window.tasks_screen.pending_delete_task_id() is None
        assert not any(task["id"] == task_id for task in window.tasks)
        window.shutdown()
        assert requested == [task_id]
