"""R49-D05: task Delete hit targets must stay inside their own rows."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QVBoxLayout, QWidget

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen
from pomodorough.ui_views import MainWindowViewMixin


def _settle() -> None:
    for _ in range(4):
        QApplication.processEvents()


def _tasks(count: int = 24) -> list[dict]:
    return [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(count)]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _board(app: QApplication, scale: float, rtl: bool = False, *, styled=True):
    previous = QFont(app.font())
    font = QFont(previous)
    font.setPointSizeF(9.0 * scale)
    app.setFont(font)
    root = QWidget()
    root.setObjectName("root")
    root.setLayoutDirection(Qt.RightToLeft if rtl else Qt.LeftToRight)
    view = TasksScreen(Strings("ar-XB" if rtl else "en"))
    QVBoxLayout(root).addWidget(view)
    if styled:
        root.setStyleSheet(MainWindowViewMixin._stylesheet() + TasksScreen.stylesheet())
    view.render(_tasks(), [], mutations_enabled=True)
    root.resize(880, 560)
    root.show()
    root.activateWindow()
    _settle()
    try:
        yield root, view
    finally:
        # Flush deferred row-resize reveals before tearing down the C++
        # widgets; firing them after deleteLater() raises "already deleted".
        _settle()
        root.close()
        root.deleteLater()
        _settle()
        app.setFont(previous)
        _settle()


def _reveal(view: TasksScreen, row: int):
    table = view.task_table
    table.scrollToItem(table.item(row, 0), QAbstractItemView.PositionAtCenter)
    # Centering the title column can push the delete column out of view
    # (RTL with wide metrics); the target cell itself must be visible.
    table.scrollTo(
        table.model().index(row, 3), QAbstractItemView.ScrollHint.EnsureVisible
    )
    _settle()
    return table.cellWidget(row, 3)


def _assert_hit(view: TasksScreen, row: int, point: QPoint) -> None:
    table = view.task_table
    # Same wide-metric RTL caveat as _reveal: the target cell must be
    # in view before mapping; no-op when already visible.
    table.scrollTo(
        table.model().index(row, 3), QAbstractItemView.ScrollHint.EnsureVisible
    )
    _settle()
    button = table.cellWidget(row, 3)
    viewport = table.viewport()
    position = button.mapTo(viewport, point)
    assert viewport.rect().contains(position)
    hit = viewport.childAt(position)
    requested = []
    view.delete_task_requested.connect(requested.append)
    try:
        assert hit is not None
        QTest.mouseClick(hit, Qt.LeftButton, pos=hit.mapFrom(viewport, position))
        assert requested == [f"task-{row}"]
        assert hit is button
        assert table.indexAt(position).row() == row
    finally:
        view.delete_task_requested.disconnect(requested.append)


def _assert_contained(view: TasksScreen, row: int) -> None:
    table = view.task_table
    button = table.cellWidget(row, 3)
    cell = table.visualRect(table.model().index(row, 3))
    bounds = QRect(button.mapTo(table.viewport(), QPoint()), button.size())
    assert cell.contains(bounds)
    assert button.height() >= button.minimumSizeHint().height()
    if row + 1 < table.rowCount():
        neighbor = table.cellWidget(row + 1, 3)
        assert not bounds.intersects(neighbor.geometry())


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_actual_delete_child_hit_testing(app, scale, rtl):
    with _board(app, scale, rtl) as (_, view):
        assert view.task_table.verticalScrollBar().maximum() > 0
        for row in (0, 1, 12, 23):
            button = _reveal(view, row)
            _assert_hit(view, row, button.rect().center())
            _assert_hit(view, row, QPoint(button.rect().center().x(), button.rect().bottom()))
            _assert_contained(view, row)


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
@pytest.mark.parametrize("change", ["font", "style", "button-font", "button-style"])
def test_live_geometry_change_keeps_scroll_and_focus_identity(app, rtl, change):
    with _board(app, 1.0, rtl, styled=change != "font") as (root, view):
        table = view.task_table
        button = _reveal(view, 12)
        button.setFocus()
        _settle()
        before = table.rowHeight(12)
        signature = view._render_signature
        if change == "font":
            font = QFont(app.font())
            font.setPointSizeF(18.0)
            app.setFont(font)
        elif change == "style":
            font = QFont(app.font())
            font.setPointSizeF(18.0)
            app.setFont(font)
            root.setStyleSheet(MainWindowViewMixin._stylesheet() + TasksScreen.stylesheet())
        elif change == "button-font":
            font = QFont(button.font())
            font.setPointSizeF(18.0)
            button.setFont(font)
        else:
            padding = button.fontMetrics().lineSpacing()
            button.setStyleSheet(f"padding-top: {padding}px; padding-bottom: {padding}px;")
        _settle()
        assert table.rowHeight(12) > before
        assert view._render_signature == signature
        assert table.cellWidget(12, 3) is button
        assert QApplication.focusWidget() is button
        assert table.viewport().rect().contains(button.geometry().center())
        _assert_contained(view, 12)
        _assert_hit(view, 12, button.rect().center())


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_unfocused_scroll_anchor_survives_style_growth_and_shrink(app, rtl):
    with _board(app, 1.0, rtl) as (root, view):
        table = view.task_table
        _reveal(view, 12)
        view.task_input.setFocus()
        _settle()
        anchor = table.rowAt(0)
        for scale in (2.0, 1.5, 1.0):
            font = QFont(app.font())
            font.setPointSizeF(9.0 * scale)
            app.setFont(font)
            root.setStyleSheet(MainWindowViewMixin._stylesheet() + TasksScreen.stylesheet())
            _settle()
            assert table.rowAt(0) == anchor
            assert QApplication.focusWidget() is view.task_input
            _assert_contained(view, anchor)
            _assert_hit(view, anchor, table.cellWidget(anchor, 3).rect().center())
            view.task_input.setFocus()


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_keyboard_delete_after_scroll_and_refresh(app, scale, rtl):
    with _board(app, scale, rtl) as (_, view):
        button = _reveal(view, 12)
        button.setFocus()
        _settle()
        tasks = _tasks()
        tasks[12]["title"] = "Renamed task with a long localized label " * 4
        view.render(tasks, [], mutations_enabled=True)
        _settle()
        assert view.task_table.cellWidget(12, 3) is button
        assert QApplication.focusWidget() is button
        assert tasks[12]["title"] in button.accessibleName()
        requested = []
        view.delete_task_requested.connect(requested.append)
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_Space)
        assert requested == ["task-12"]
        QTest.keyClick(button, Qt.Key_Tab)
        assert QApplication.focusWidget() is not button
        neighbor = _reveal(view, 13)
        neighbor.setFocus()
        _settle()
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_Space)
        assert requested == ["task-12", "task-13"]
        _reveal(view, 12)
        _assert_contained(view, 12)


def test_wrapped_text_drives_row_height_after_viewport_resize(app):
    with _board(app, 1.0, styled=False) as (root, view):
        table = view.task_table
        tasks = _tasks()
        tasks[0]["title"] = "A long task title with many words to wrap in the task column " * 3
        view.render(tasks, [], mutations_enabled=True)
        _settle()
        wide_height = table.rowHeight(0)
        root.resize(root.width() // 2, root.height())
        _settle()
        # Windows font rounding can keep wrapped height equal; never shrink.
        assert table.rowHeight(0) >= wide_height
        assert table.rowHeight(0) >= table.rowHeight(1)
        _reveal(view, 0)
        _assert_contained(view, 0)
        _assert_hit(view, 0, table.cellWidget(0, 3).rect().center())


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_disabled_delete_hit_target_does_not_emit(app, rtl):
    with _board(app, 2.0, rtl) as (_, view):
        view.render(_tasks(), [], mutations_enabled=False)
        _settle()
        button = _reveal(view, 12)
        position = button.mapTo(view.task_table.viewport(), button.rect().center())
        hit = view.task_table.viewport().childAt(position)
        assert hit is button
        assert not hit.isEnabled()
        requested = []
        view.delete_task_requested.connect(requested.append)
        QTest.mouseClick(hit, Qt.LeftButton)
        QTest.keyClick(hit, Qt.Key_Space)
        assert requested == []
        _assert_contained(view, 12)
