"""Tasks screen: F2, double-click, and the context menu rename a task inline."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen


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
    renamed: list[tuple[str, str]] = []
    view.rename_task_requested.connect(lambda task_id, title: renamed.append((task_id, title)))
    view.renamed = renamed
    yield view
    view.close()
    _settle()
    view.deleteLater()
    _settle()
    del app


def _editor(view: TasksScreen, row: int) -> QLineEdit:
    editor = view.task_table.cellWidget(row, 0)
    assert isinstance(editor, QLineEdit)
    assert QApplication.focusWidget() is editor
    return editor


def _commit(editor: QLineEdit, text: str | None) -> None:
    if text is not None:
        editor.selectAll()
        if text:
            QTest.keyClicks(editor, text)
        else:
            editor.clear()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    _settle()


def _press_f2(view: TasksScreen, row: int) -> QLineEdit:
    table = view.task_table
    table.setFocus()
    table.setCurrentCell(row, 0)
    _settle()
    QTest.keyClick(table, Qt.Key.Key_F2)
    _settle()
    return _editor(view, row)


def test_f2_on_current_row_commits_new_title(screen):
    editor = _press_f2(screen, 1)
    assert editor.text() == "Task 1"
    _commit(editor, "Renamed")
    assert screen.renamed == [("task-1", "Renamed")]
    assert screen.task_table.cellWidget(1, 0) is None
    # The owner re-renders from the store; the cell keeps the old title.
    assert screen.task_table.item(1, 0).text() == "Task 1"


def test_f2_on_focused_delete_button_renames_that_row(screen):
    button = screen.task_table.cellWidget(2, 3)
    button.setFocus()
    _settle()
    QTest.keyClick(button, Qt.Key.Key_F2)
    _settle()
    _commit(_editor(screen, 2), "Via button")
    assert screen.renamed == [("task-2", "Via button")]


def test_double_click_on_title_cell_opens_editor(screen):
    table = screen.task_table
    rect = table.visualItemRect(table.item(0, 0))
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    _settle()
    _commit(_editor(screen, 0), "Clicked")
    assert screen.renamed == [("task-0", "Clicked")]


def test_double_click_on_other_columns_does_not_edit(screen):
    table = screen.task_table
    rect = table.visualItemRect(table.item(0, 1))
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    _settle()
    assert table.cellWidget(0, 0) is None
    assert table.cellWidget(0, 1) is None
    assert screen.renamed == []


def test_context_menu_offers_rename_for_row(screen):
    menu = screen._task_context_menu(1)
    assert [action.text() for action in menu.actions()] == ["Rename"]
    menu.actions()[0].trigger()
    _settle()
    _commit(_editor(screen, 1), "From menu")
    assert screen.renamed == [("task-1", "From menu")]
    assert screen._task_context_menu(-1) is None


def test_escape_cancels_without_request(screen):
    editor = _press_f2(screen, 1)
    editor.selectAll()
    QTest.keyClicks(editor, "Discarded")
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    _settle()
    assert screen.renamed == []
    assert screen.task_table.cellWidget(1, 0) is None
    assert screen.task_table.item(1, 0).text() == "Task 1"


@pytest.mark.parametrize("text", [None, "Task 1"])
def test_unchanged_title_does_not_request(screen, text):
    editor = _press_f2(screen, 1)
    _commit(editor, text)
    assert screen.renamed == []
    assert screen.task_table.item(1, 0).text() == "Task 1"


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_title_requests_so_the_controller_can_refuse_it(screen, text):
    # UX round 2 B4: the controller answers with the blank-name sentence.
    editor = _press_f2(screen, 1)
    _commit(editor, text)
    assert screen.renamed == [("task-1", text)]
    assert screen.task_table.item(1, 0).text() == "Task 1"


def test_delete_keys_edit_text_while_renaming(screen):
    deleted: list[str] = []
    screen.delete_task_requested.connect(deleted.append)
    editor = _press_f2(screen, 1)
    editor.setCursorPosition(len(editor.text()))
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    QTest.keyClick(editor, Qt.Key.Key_Delete)
    _settle()
    assert editor.text() == "Task "
    assert screen.pending_delete_task_id() is None
    _commit(editor, None)
    assert screen.renamed == [("task-1", "Task ")]
    assert deleted == []


def test_rename_flushes_pending_delete_first(screen):
    order: list[str] = []
    screen.delete_task_requested.connect(lambda _id: order.append("delete"))
    screen.rename_task_requested.connect(lambda *_args: order.append("rename"))
    screen.task_table.cellWidget(0, 3).click()
    _settle()
    row = next(
        row
        for row in range(screen.task_table.rowCount())
        if screen.task_table.cellWidget(row, 3).property("taskId") == "task-2"
    )
    editor = _press_f2(screen, row)
    _commit(editor, "After delete")
    assert order == ["delete", "rename"]
    assert screen.renamed == [("task-2", "After delete")]


def test_render_with_new_data_drops_open_editor(screen):
    editor = _press_f2(screen, 1)
    editor.selectAll()
    QTest.keyClicks(editor, "Half typed")
    screen.render(_tasks(4), [], mutations_enabled=True)
    _settle()
    assert screen.task_table.cellWidget(1, 0) is None
    assert screen.task_table.item(1, 0).text() == "Task 1"
    assert screen.renamed == []


def test_render_with_same_data_keeps_editor_open(screen):
    editor = _press_f2(screen, 1)
    screen.render(_tasks(), [], mutations_enabled=True)
    _settle()
    assert screen.task_table.cellWidget(1, 0) is editor


def test_disabled_rows_do_not_rename(screen):
    screen.render(_tasks(), [], mutations_enabled=False)
    _settle()
    table = screen.task_table
    table.setFocus()
    table.setCurrentCell(1, 0)
    QTest.keyClick(table, Qt.Key.Key_F2)
    _settle()
    assert table.cellWidget(1, 0) is None
    assert screen._task_context_menu(1) is None
