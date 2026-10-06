"""R43-D08: focused task Delete must survive history/count/title refresh."""

from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen


def _tasks() -> list[dict]:
    return [
        {"id": "task-1", "title": "First"},
        {"id": "task-2", "title": "Second"},
        {"id": "task-3", "title": "Third"},
    ]


def _completed(task_id: str) -> dict:
    return {
        "id": f"history-{task_id}",
        "taskId": task_id,
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "completedAt": datetime.now().astimezone().isoformat(),
    }


@pytest.fixture
def screen():
    app = QApplication.instance() or QApplication([])
    view = TasksScreen(Strings("en"))
    view.resize(400, 300)
    view.show()
    app.processEvents()
    yield view
    view.close()
    app.processEvents()


def _focus_delete(screen: TasksScreen, row: int):
    button = screen.task_table.cellWidget(row, 3)
    button.setFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() is button
    return button


def test_history_refresh_keeps_focused_delete(screen: TasksScreen) -> None:
    tasks = _tasks()
    screen.render(tasks, [], mutations_enabled=True)
    before = _focus_delete(screen, 1)
    screen.render(tasks, [_completed("task-3")], mutations_enabled=True)
    QApplication.processEvents()
    after = screen.task_table.cellWidget(1, 3)
    assert after is before
    assert QApplication.focusWidget() is before


def test_count_refresh_keeps_focused_delete(screen: TasksScreen) -> None:
    tasks = _tasks()
    screen.render(tasks, [], mutations_enabled=True)
    before = _focus_delete(screen, 0)
    assert screen.task_table.item(0, 1).text() == "0"
    screen.render(tasks, [_completed("task-1")], mutations_enabled=True)
    QApplication.processEvents()
    assert screen.task_table.item(0, 1).text() == "1"
    after = screen.task_table.cellWidget(0, 3)
    assert after is before
    assert QApplication.focusWidget() is before


def test_title_refresh_keeps_focused_delete(screen: TasksScreen) -> None:
    screen.render(_tasks(), [], mutations_enabled=True)
    before = _focus_delete(screen, 1)
    renamed = _tasks()
    renamed[1] = {"id": "task-2", "title": "Second renamed"}
    screen.render(renamed, [], mutations_enabled=True)
    QApplication.processEvents()
    after = screen.task_table.cellWidget(1, 3)
    assert after is before
    assert QApplication.focusWidget() is before
    assert "Second renamed" in after.accessibleName()
    assert screen.task_table.item(1, 0).text() == "Second renamed"


def test_deleted_focused_row_moves_to_next_neighbor(
    screen: TasksScreen,
) -> None:
    screen.render(_tasks(), [], mutations_enabled=True)
    _focus_delete(screen, 1)
    remaining = [task for task in _tasks() if task["id"] != "task-2"]
    screen.render(remaining, [], mutations_enabled=True)
    QApplication.processEvents()
    neighbor = screen.task_table.cellWidget(1, 3)
    assert neighbor is not None
    assert screen.task_table.item(1, 0).text() == "Third"
    assert QApplication.focusWidget() is neighbor


def test_deleted_last_row_moves_to_previous_neighbor(
    screen: TasksScreen,
) -> None:
    screen.render(_tasks(), [], mutations_enabled=True)
    _focus_delete(screen, 2)
    screen.render(_tasks()[:2], [], mutations_enabled=True)
    QApplication.processEvents()
    neighbor = screen.task_table.cellWidget(1, 3)
    assert neighbor is not None
    assert screen.task_table.item(1, 0).text() == "Second"
    assert QApplication.focusWidget() is neighbor


def test_deleted_only_row_focuses_task_input(screen: TasksScreen) -> None:
    screen.render([{"id": "solo", "title": "Solo"}], [], mutations_enabled=True)
    _focus_delete(screen, 0)
    screen.render([], [], mutations_enabled=True)
    QApplication.processEvents()
    assert screen.task_table.rowCount() == 0
    assert QApplication.focusWidget() is screen.task_input
