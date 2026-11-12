"""UX round 2 A1 (DSK-15): the task undo window has no timeout and ends when the user moves on."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen

from test_r52_d06_task_header_wrap import _process, _scaled_font, _tasks_window

@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def screen(app):
    view = TasksScreen(Strings("en"))
    view.resize(480, 360)
    view.show()
    _process(app)
    tasks = [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(3)]
    view.render(tasks, [], mutations_enabled=True)
    _process(app)
    view.deleted = []

    def delete(task_id: str) -> None:
        # Like the real owner: the completed delete re-renders without the task.
        view.deleted.append(task_id)
        remaining = [task for task in tasks if task["id"] not in view.deleted]
        view.render(remaining, [], mutations_enabled=True)

    view.delete_task_requested.connect(delete)
    yield view
    view.close()
    _process(app)
    view.deleteLater()


def _delete_row(app, table, row: int) -> str:
    button = table.cellWidget(row, 3)
    task_id = button.property("taskId")
    button.setFocus(Qt.FocusReason.MouseFocusReason)
    button.click()
    _process(app, 20)
    return task_id


def _row_tops(window: QWidget, table) -> list[int]:
    return [
        table.cellWidget(row, 3).mapTo(window, QPoint(0, 0)).y()
        for row in range(table.rowCount())
    ]


def _rect_in(window: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def _docked(window) -> bool:
    return window.tasks_screen.undo_banner.parentWidget() is window.tasks_page


def _reveal_delete_button(app, window, row: int) -> QPoint:
    """Scroll the row into view and return a window point on its delete button."""
    button = window.task_table.cellWidget(row, 3)
    button.setFocus(Qt.FocusReason.TabFocusReason)
    _process(app, 20)
    visible = button.visibleRegion().boundingRect()
    assert not visible.isEmpty()
    return button.mapTo(window, visible.center())


def _populate(app, window, count: int = 5) -> None:
    for index in range(count):
        window._add_task(f"Round two task {index}")
    _process(app, 20)


# --- The undo window itself -------------------------------------------------


class _StartedTimers:
    """Every timer the application starts, wherever its owner lives (L8)."""

    def __init__(self) -> None:
        self.timers: list[QTimer] = []
        self.single_shots: list[Callable[[], object]] = []

    def expire_all(self) -> None:
        for timer in self.timers:
            # A layout timer may belong to a widget that is already gone.
            if shiboken6.isValid(timer):
                timer.timeout.emit()
        for callback in self.single_shots:
            callback()

    def count(self) -> int:
        return len(self.timers) + len(self.single_shots)


@contextmanager
def _recorded_timers() -> Iterator[_StartedTimers]:
    started = _StartedTimers()
    start = QTimer.start

    def record_start(timer: QTimer, *args) -> None:
        started.timers.append(timer)
        start(timer, *args)

    def record_single_shot(*args) -> None:
        # The callback is the last argument in every singleShot overload.
        started.single_shots.append(args[-1])

    with (
        patch.object(QTimer, "start", record_start),
        patch.object(QTimer, "singleShot", record_single_shot),
    ):
        yield started


def test_no_timer_anywhere_in_the_window_ends_the_undo_window(app, tmp_path):
    # L8: layout timers may start. A timeout owned by TasksPage,
    # TaskUndoLifetime, the window or a static single shot would end the
    # undo window, so every started timer is expired at once.
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _populate(app, window, 2)
        with _recorded_timers() as started:
            task_id = _delete_row(app, window.task_table, 0)
            _process(app, 20)
            started.expire_all()
            _process(app, 20)
        assert window.tasks_screen.pending_delete_task_id() == task_id
        assert window.tasks_screen.undo_banner.isVisible()
        assert task_id in [task["id"] for task in window.tasks]


def test_recorded_timers_see_object_and_static_timers(app):
    # The recorder itself: without this the timer tests prove nothing.
    fired: list[str] = []
    owner = QTimer()
    owner.timeout.connect(lambda: fired.append("object"))
    with _recorded_timers() as started:
        owner.start(60_000)
        QTimer.singleShot(60_000, lambda: fired.append("static"))
    owner.stop()
    assert started.count() == 2
    started.expire_all()
    assert fired == ["object", "static"]


def test_undo_has_no_timeout_that_can_expire(app, screen):
    # L12: no wall-clock wait. Any timer the delete starts is expired at
    # once; the old five second limit ended the undo window this way.
    with _recorded_timers() as started:
        _delete_row(app, screen.task_table, 0)
        started.expire_all()
        _process(app)
    assert screen.deleted == []
    assert screen.pending_delete_task_id() == "task-0"
    assert screen.undo_banner.isVisible()
    screen.undo_button.click()
    _process(app)
    assert screen.task_table.rowCount() == 3
    assert screen.deleted == []


def test_dismiss_completes_the_delete_through_the_delete_request(app, screen):
    _delete_row(app, screen.task_table, 1)
    dismiss = screen.findChild(QWidget, "undoDismissButton")
    assert dismiss is not None and dismiss.isVisible()
    assert dismiss.text() == "DELETE NOW"
    assert dismiss.accessibleName() == "Delete Task 1 now"
    assert dismiss.focusPolicy() & Qt.FocusPolicy.TabFocus
    dismiss.click()
    _process(app)
    assert screen.deleted == ["task-1"]
    assert screen.pending_delete_task_id() is None
    assert not screen.undo_banner.isVisible()


def test_dismiss_from_the_keyboard_returns_focus_to_the_board(app, screen):
    screen.activateWindow()
    _delete_row(app, screen.task_table, 1)
    dismiss = screen.findChild(QWidget, "undoDismissButton")
    assert dismiss is not None
    dismiss.setFocus()
    _process(app)
    QTest.keyClick(dismiss, Qt.Key.Key_Space)
    _process(app)
    assert screen.deleted == ["task-1"]
    # L3: the row that took the deleted row's place, not the first row.
    neighbour = screen.task_table.cellWidget(1, 3)
    assert neighbour.property("taskId") == "task-2"
    assert app.focusWidget() is neighbour


def test_dismiss_after_deleting_the_last_row_focuses_the_new_last_row(app, screen):
    screen.activateWindow()
    _delete_row(app, screen.task_table, 2)
    dismiss = screen.findChild(QWidget, "undoDismissButton")
    dismiss.setFocus()
    _process(app)
    QTest.keyClick(dismiss, Qt.Key.Key_Space)
    _process(app)
    neighbour = screen.task_table.cellWidget(1, 3)
    assert neighbour.property("taskId") == "task-1"
    assert app.focusWidget() is neighbour


def test_undo_is_announced_to_screen_readers(app, screen):
    with patch.object(QAccessible, "updateAccessibility") as update:
        _delete_row(app, screen.task_table, 1)
    announced = [
        call.args[0].message()
        for call in update.call_args_list
        if isinstance(call.args[0], QAccessibleAnnouncementEvent)
    ]
    assert announced == ["Deleted “Task 1”. Undo is available."]


def test_standalone_banner_sits_below_the_rows(app, screen):
    table = screen.task_table
    before = _row_tops(screen, table)
    _delete_row(app, table, 0)
    assert _row_tops(screen, table) == before[:-1]
    banner = _rect_in(screen, screen.undo_banner)
    assert banner.top() >= _rect_in(screen, table).bottom()


# --- Placement in the production window --------------------------------------


@pytest.mark.parametrize("size", [(640, 360), (900, 700)])
def test_rows_keep_their_place_when_the_banner_appears(app, tmp_path, size):
    with _scaled_font(app, 9.0), _tasks_window(app, tmp_path, "en", *size) as window:
        _populate(app, window)
        table = window.task_table
        before = _row_tops(window, table)
        _delete_row(app, table, 0)
        assert window.tasks_screen.undo_banner.isVisible()
        assert _row_tops(window, table) == before[:-1]


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("size", [(640, 360), (1, 1), (900, 700)])
def test_banner_docks_below_the_rows_and_clear_of_task_controls(app, tmp_path, locale, size):
    with _scaled_font(app, 9.0), _tasks_window(app, tmp_path, locale, *size) as window:
        _populate(app, window)
        _delete_row(app, window.task_table, 0)
        page = window.tasks_page
        banner = _rect_in(window, window.tasks_screen.undo_banner)
        viewport = _rect_in(window, page.viewport())
        assert banner.top() > viewport.bottom()
        assert window.rect().contains(banner)
        focused = app.focusWidget()
        assert focused is window.task_table.cellWidget(0, 3)
        for control in (window.task_input, window.add_task_button, focused):
            assert not banner.intersects(_rect_in(window, control))
        for action in (window.tasks_screen.undo_button, window.tasks_screen.undo_dismiss_button):
            assert window.childAt(_rect_in(window, action).center()) is action
        assert page.verticalScrollBar().maximum() > 0 or size == (900, 700)


def test_banner_returns_to_the_page_when_the_window_ends(app, tmp_path):
    with _scaled_font(app, 9.0), _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _populate(app, window)
        page = window.tasks_page
        height_before = page.viewport().height()
        _delete_row(app, window.task_table, 0)
        assert page.viewport().height() < height_before
        window.tasks_screen.undo_button.click()
        _process(app, 20)
        assert page.viewport().height() == height_before
        assert not window.tasks_screen.undo_banner.isVisible()
        assert window.task_table.rowCount() == 5


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_large_font_in_a_small_window_keeps_a_whole_row(app, tmp_path, locale):
    with _scaled_font(app, 30.0), _tasks_window(app, tmp_path, locale, 1, 1) as window:
        _populate(app, window)
        table = window.task_table
        _reveal_delete_button(app, window, 0)
        _delete_row(app, table, 0)
        page = window.tasks_page
        screen = window.tasks_screen
        assert screen.undo_banner.isVisible()
        assert screen.pending_delete_task_id() is not None
        banner = _rect_in(screen, screen.undo_banner)
        assert _docked(window) or banner.top() >= _rect_in(screen, table).bottom()
        tallest_row = max(table.rowHeight(row) for row in range(table.rowCount()))
        whole_row_fits = page.viewport().height() >= tallest_row
        assert whole_row_fits or not _docked(window)
        assert whole_row_fits or page.viewportMargins().bottom() == 0
        focused = app.focusWidget()
        assert focused is table.cellWidget(0, 3)
        assert not focused.visibleRegion().isEmpty()


@pytest.mark.parametrize("points", [9.0, 30.0])
def test_tab_from_the_last_row_reaches_undo_then_dismiss(app, tmp_path, points):
    with _scaled_font(app, points), _tasks_window(app, tmp_path, "en", 640, 360) as window:
        window.activateWindow()
        _populate(app, window, 3)
        table = window.task_table
        _delete_row(app, table, 0)
        last = table.cellWidget(table.rowCount() - 1, 3)
        last.setFocus(Qt.FocusReason.TabFocusReason)
        _process(app)
        QTest.keyClick(last, Qt.Key.Key_Tab)
        _process(app)
        assert app.focusWidget() is window.tasks_screen.undo_button
        QTest.keyClick(app.focusWidget(), Qt.Key.Key_Tab)
        _process(app)
        assert app.focusWidget().objectName() == "undoDismissButton"
        visible = app.focusWidget().visibleRegion().boundingRect()
        assert visible == app.focusWidget().rect()


# --- What ends the undo window ------------------------------------------------


def _pending_delete(app, window) -> tuple[str, list[str]]:
    _populate(app, window, 2)
    requested: list[str] = []
    window.tasks_screen.delete_task_requested.connect(requested.append)
    task_id = _delete_row(app, window.task_table, 0)
    assert window.tasks_screen.pending_delete_task_id() == task_id
    assert requested == []
    return task_id, requested


def _assert_delete_completed(window, task_id: str, requested: list[str]) -> None:
    assert requested == [task_id]
    assert window.tasks_screen.pending_delete_task_id() is None
    assert not any(task["id"] == task_id for task in window.tasks)


def test_leaving_the_tasks_screen_completes_the_delete(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window.screen_buttons[0].click()
        _process(app)
        _assert_delete_completed(window, task_id, requested)


@pytest.mark.parametrize(
    "state",
    [
        Qt.ApplicationState.ApplicationInactive,
        Qt.ApplicationState.ApplicationHidden,
        Qt.ApplicationState.ApplicationSuspended,
        Qt.ApplicationState.ApplicationActive,
    ],
)
def test_application_state_changes_keep_the_undo_window(app, tmp_path, state):
    # B2: switching to another application is not "moving on". The window
    # stays open, so Undo stays; hide, minimize and close still complete.
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        QGuiApplication.instance().applicationStateChanged.emit(state)
        _process(app)
        assert requested == []
        assert window.tasks_screen.pending_delete_task_id() == task_id
        assert window.tasks_screen.undo_banner.isVisible()
        assert any(task["id"] == task_id for task in window.tasks)


def test_window_deactivation_keeps_the_undo_window(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
        _process(app)
        assert requested == []
        assert window.tasks_screen.pending_delete_task_id() == task_id


def test_hiding_the_window_completes_the_delete(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window.hide()
        _process(app)
        _assert_delete_completed(window, task_id, requested)


def test_minimizing_the_window_completes_the_delete(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window.setWindowState(Qt.WindowState.WindowMinimized)
        _process(app)
        _assert_delete_completed(window, task_id, requested)


# --- Nothing acts on the hidden task -------------------------------------------


def test_primary_timer_action_completes_the_delete_before_it_starts(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _populate(app, window, 2)
        table = window.task_table
        selected = window.settings.get("selectedTaskId")
        row = next(
            row
            for row in range(table.rowCount())
            if table.cellWidget(row, 3).property("taskId") == selected
        )
        requested: list[str] = []
        window.tasks_screen.delete_task_requested.connect(requested.append)
        assert _delete_row(app, table, row) == selected
        assert requested == []
        window._primary_action()
        _process(app)
        _assert_delete_completed(window, selected, requested)
        assert window.timer["status"] == "running"
        assert window.timer.get("taskId") != selected
        assert window.settings.get("selectedTaskId") is None


def test_user_timer_command_completes_the_delete_first(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window._issue("start")
        _process(app)
        _assert_delete_completed(window, task_id, requested)


def test_automatic_timer_command_keeps_the_undo_window(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window._issue("finish", automatic=True)
        _process(app)
        assert requested == []
        assert window.tasks_screen.pending_delete_task_id() == task_id
