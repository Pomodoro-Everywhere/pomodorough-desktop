"""R50-D05: action column follows button-only font growth and shrink."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen
from pomodorough.ui_views import MainWindowViewMixin


def _settle() -> None:
    for _ in range(6):
        QApplication.processEvents()


def _tasks(count: int = 8) -> list[dict]:
    return [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(count)]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _board(app: QApplication, rtl: bool, count: int = 8):
    previous = QFont(app.font())
    font = QFont(previous)
    font.setPointSizeF(9.0)
    app.setFont(font)
    root = QWidget()
    root.setLayoutDirection(Qt.RightToLeft if rtl else Qt.LeftToRight)
    view = TasksScreen(Strings("ar-XB" if rtl else "en"))
    QVBoxLayout(root).addWidget(view)
    root.setStyleSheet(MainWindowViewMixin._stylesheet() + TasksScreen.stylesheet())
    view.render(_tasks(count), [], mutations_enabled=True)
    root.resize(880, 560)
    root.show()
    _settle()
    try:
        yield root, view
    finally:
        root.close()
        root.deleteLater()
        _settle()
        app.setFont(previous)
        _settle()


def _assert_contained(view: TasksScreen, row: int) -> None:
    table = view.task_table
    button = table.cellWidget(row, 3)
    cell = table.visualRect(table.model().index(row, 3))
    bounds = QRect(button.mapTo(table.viewport(), QPoint(0, 0)), button.size())
    assert cell.contains(bounds)


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_button_growth_widens_column_to_hint(app, rtl):
    with _board(app, rtl) as (_, view):
        table = view.task_table
        before = table.columnWidth(3)
        button = table.cellWidget(0, 3)
        owned = button
        font = QFont(button.font())
        font.setPointSizeF(24.0)
        button.setFont(font)
        _settle()
        assert table.columnWidth(3) > before
        assert table.columnWidth(3) >= button.minimumSizeHint().width()
        assert table.cellWidget(0, 3) is owned
        _assert_contained(view, 0)
        viewport = table.viewport()
        pos = button.mapTo(viewport, button.rect().center())
        assert viewport.childAt(pos) is button


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_button_shrink_returns_column(app, rtl):
    with _board(app, rtl) as (_, view):
        table = view.task_table
        before = table.columnWidth(3)
        button = table.cellWidget(0, 3)
        font = QFont(button.font())
        font.setPointSizeF(24.0)
        button.setFont(font)
        _settle()
        grown = table.columnWidth(3)
        assert grown > before
        small = QFont(button.font())
        small.setPointSizeF(9.0)
        button.setFont(small)
        _settle()
        assert table.columnWidth(3) < grown
        assert table.columnWidth(3) >= button.minimumSizeHint().width()
        _assert_contained(view, 0)


def test_focused_button_survives_growth(app):
    with _board(app, False, count=24) as (_, view):
        table = view.task_table
        table.scrollToItem(table.item(12, 0))
        _settle()
        button = table.cellWidget(12, 3)
        button.setFocus()
        _settle()
        signature = view._render_signature
        font = QFont(button.font())
        font.setPointSizeF(24.0)
        button.setFont(font)
        _settle()
        assert view._render_signature == signature
        assert table.cellWidget(12, 3) is button
        assert QApplication.focusWidget() is button
        assert table.columnWidth(3) >= button.minimumSizeHint().width()
        _assert_contained(view, 12)
        assert table.viewport().rect().contains(button.geometry().center())


def test_unfocused_scroll_anchor_and_no_loop(app):
    with _board(app, False, count=24) as (_, view):
        table = view.task_table
        table.scrollToItem(table.item(12, 0))
        _settle()
        view.task_input.setFocus()
        _settle()
        anchor = table.rowAt(0)
        v_before = table.verticalScrollBar().value()
        button = table.cellWidget(0, 3)
        font = QFont(button.font())
        font.setPointSizeF(24.0)
        button.setFont(font)
        _settle()
        assert table.rowAt(0) == anchor
        assert table.verticalScrollBar().value() == v_before
        assert QApplication.focusWidget() is view.task_input
        assert not view._row_resize_timer.isActive()
        assert view._in_layout_refresh is False
        stable = table.columnWidth(3)
        _settle()
        assert table.columnWidth(3) == stable
