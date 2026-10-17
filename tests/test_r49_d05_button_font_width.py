"""R49-D05 residual: button-only font growth stays vertical, width documented."""

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
    for _ in range(4):
        QApplication.processEvents()


def _tasks(count: int = 8) -> list[dict]:
    return [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(count)]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _board(app: QApplication):
    previous = QFont(app.font())
    font = QFont(previous)
    font.setPointSizeF(9.0)
    app.setFont(font)
    root = QWidget()
    root.setLayoutDirection(Qt.LeftToRight)
    view = TasksScreen(Strings("en"))
    QVBoxLayout(root).addWidget(view)
    root.setStyleSheet(MainWindowViewMixin._stylesheet() + TasksScreen.stylesheet())
    view.render(_tasks(), [], mutations_enabled=True)
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


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_button_only_font_growth_stays_vertically_contained(app, rtl):
    with _board(app) as (_, view):
        if rtl:
            view.setLayoutDirection(Qt.RightToLeft)
            _settle()
        table = view.task_table
        before_height = table.rowHeight(0)
        before_width = table.columnWidth(3)
        button = table.cellWidget(0, 3)
        before_button_height = button.height()
        font = QFont(button.font())
        font.setPointSizeF(24.0)
        button.setFont(font)
        _settle()
        # Vertical geometry follows the button-only change.
        assert table.rowHeight(0) > before_height
        assert button.height() > before_button_height
        cell = table.visualRect(table.model().index(0, 3))
        bounds = QRect(button.mapTo(table.viewport(), QPoint(0, 0)), button.size())
        assert cell.contains(bounds)
        # Adjacent rows do not overlap even when the action column is narrow.
        neighbor = table.cellWidget(1, 3)
        assert not button.geometry().intersects(neighbor.geometry())
        # Documented limitation: the action column keeps its old width while
        # the button hint grows, so the hint can exceed the column.
        assert table.columnWidth(3) == before_width
        assert button.minimumSizeHint().width() > table.columnWidth(3)
        # Hit-testing still delivers row zero at the button center.
        viewport = table.viewport()
        position = button.mapTo(viewport, button.rect().center())
        assert viewport.childAt(position) is button
