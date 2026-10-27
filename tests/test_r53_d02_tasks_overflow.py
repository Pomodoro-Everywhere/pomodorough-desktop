"""Production Tasks page overflow must recover real keyboard and pointer targets."""

from __future__ import annotations

import pytest
from collections.abc import Iterator
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_r52_d06_task_header_wrap import (
    _assert_header_intact,
    _dump,
    _process,
    _scaled_font,
    _tasks_window,
)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _populate(window):
    for index in range(8):
        window.task_input.setText(f"Populated production task {index}")
        window._add_task()
    window._render()


def _assert_hit_target(window, widget):
    assert widget.visibleRegion().contains(widget.rect())
    point = widget.mapTo(window, widget.rect().center())
    assert window.rect().contains(point)
    assert window.childAt(point) is widget


def _tab_to(qapp, start, target, *, backwards=False):
    start.setFocus(Qt.FocusReason.TabFocusReason)
    _process(qapp)
    for _ in range(100):
        if qapp.focusWidget() is target:
            return
        modifiers = Qt.KeyboardModifier.ShiftModifier if backwards else Qt.KeyboardModifier.NoModifier
        QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Tab, modifiers)
        _process(qapp)
    pytest.fail(f"Tab did not reach {target.accessibleName()}")


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("points", [18.0, 24.0, 30.0])
def test_short_populated_page_reaches_delete(app, tmp_path, locale, points):
    with _scaled_font(app, points):
        with _tasks_window(app, tmp_path, locale, 640, 360) as window:
            _populate(window)
            _process(app, 20)
            assert window.size().toTuple() == (640, 360)
            _assert_header_intact(window, "populated")
            _tab_to(app, window.task_input, window.add_task_button)
            _assert_hit_target(window, window.add_task_button)
            table = window.task_table
            for row in range(table.rowCount()):
                delete = table.cellWidget(row, 3)
                _tab_to(app, app.focusWidget(), delete)
                assert table.viewport().height() >= delete.height()
                _assert_hit_target(window, delete)
            before = table.rowCount()
            QTest.keyClick(delete, Qt.Key.Key_Space)
            _process(app, 20)
            assert table.rowCount() == before - 1
            _assert_hit_target(window, app.focusWidget())
            window.screen_buttons[1].setFocus(Qt.FocusReason.ShortcutFocusReason)
            _process(app)
            assert app.focusWidget() is window.screen_buttons[1]
            QTest.keyClick(window.screen_buttons[1], Qt.Key.Key_Space)
            _process(app)
            assert window.page_stack.currentWidget() is window.tasks_page


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_live_font_growth_shrink_preserves_focused_delete(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _tasks_window(app, tmp_path, locale, 640, 360) as window:
            _populate(window)
            _process(app, 20)
            table = window.task_table
            delete = table.cellWidget(table.rowCount() - 1, 3)
            _tab_to(app, window.task_input, delete)
            before = _dump(window.store)
            minimum = table.minimumHeight()
            for points in (18.0, 24.0, 30.0, 9.0):
                font = QFont(QApplication.font())
                font.setPointSizeF(points)
                QApplication.setFont(font)
                _process(app, 25)
                assert app.focusWidget() is delete
                _assert_header_intact(window, str(points))
                _assert_hit_target(window, delete)
            assert table.minimumHeight() <= minimum + table.frameWidth() * 2
            assert _dump(window.store) == before
            _tab_to(app, delete, window.task_input, backwards=True)
            _assert_hit_target(window, window.task_input)
            assert window.task_input.mapTo(window, QPoint()).y() >= 0
