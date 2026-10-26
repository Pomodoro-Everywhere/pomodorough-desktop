"""R52-D04: stylesheet pixel dimensions scale exactly once."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from pomodorough.sound import CompletionSound
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from pomodorough.ui_views import _app_font_scale
from pomodorough.ui_views import MainWindowViewMixin
from test_ui import FakeCloud, MemorySecretStore
import pomodorough.ui_views as ui_views


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication) -> None:
    for _ in range(16):
        app.processEvents()


@contextmanager
def _scaled_font(app: QApplication, point_size: float):
    previous = QFont(QApplication.font())
    scaled = QFont(previous)
    scaled.setPointSizeF(point_size)
    QApplication.setFont(scaled)
    _process(app)
    try:
        yield
    finally:
        QApplication.setFont(previous)
        _process(app)


def _sheet_for_scale(scale: float) -> str:
    with patch.object(ui_views, "_app_font_scale", return_value=scale):
        return MainWindowViewMixin._stylesheet()


def test_px_dimensions_scale_once() -> None:
    base = _sheet_for_scale(1.0)
    assert "min-height: 36px" in base
    assert "min-width: 72px" in base
    assert _app_font_scale() >= 0  # keeps helper referenced
    grown = _sheet_for_scale(2.0)
    assert "min-height: 72px" in grown
    assert "min-height: 144px" not in grown
    assert "min-width: 144px" in grown
    assert "max-width: 300px" in grown
    assert "min-width: 128px" in grown
    assert "min-height: 58px" in grown
    assert grown.count("144px") == 1
    mid = _sheet_for_scale(1.5)
    assert "min-height: 54px" in mid
    assert "min-width: 108px" in mid
    assert "min-height: 44px" in mid


def test_no_second_order_collision() -> None:
    scale = 150.0 / 72.0
    sheet = _sheet_for_scale(scale)
    assert sheet.count("150px") == 1
    assert sheet.count("312px") == 1


@contextmanager
def _tasks_window(app, tmp_path, locale):
    store = Store(tmp_path / "state.sqlite3", iroh_secret_store=MemorySecretStore())
    store.set_replication_mode("offline")
    patches = (
        patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False),
        patch.object(CompletionSound, "play", return_value=False),
    )
    for entered in patches:
        entered.__enter__()
    try:
        window = MainWindow(store, FakeCloud(authenticated=False), QIcon(), locale=locale)
    except BaseException:
        for entered in patches:
            entered.__exit__(None, None, None)
        raise
    window.tick_timer.stop()
    window.sync_timer.stop()
    window.notice.disconnect(window._show_notice)
    window.resize(640, 360)
    window.show()
    _process(app)
    try:
        yield window, store
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()
        for entered in patches:
            entered.__exit__(None, None, None)


def _button_table_state(window):
    button = window.add_task_button
    table = window.task_table
    button_rect = QRect(button.mapTo(window, QPoint(0, 0)), button.size())
    table_rect = QRect(table.mapTo(window, QPoint(0, 0)), table.size())
    center = button.mapTo(window, button.rect().center())
    return button, table_rect, button_rect, center, window.childAt(center)


def _titles(store) -> list[str]:
    return [op.get("title", "") for op in store.load()["pendingTasks"] if "title" in op]


def test_add_button_hit_test_mouse_keyboard_2x(app, tmp_path) -> None:
    with _scaled_font(app, 9.0):
        with _tasks_window(app, tmp_path, "en") as (window, store):
            window._show_screen(1)
            window.task_input.setText("R52-D04 seed task")
            QTest.keyClick(window.task_input, Qt.Key.Key_Return)
            _process(app)
            font = QFont(QApplication.font())
            font.setPointSizeF(18.0)
            QApplication.setFont(font)
            window.resize(640, 360)
            _process(app)
            window._show_screen(1)
            _process(app)
            button, table_rect, button_rect, center, hit = _button_table_state(window)
            assert button.height() <= 110
            assert hit is button
            before = len(store.load()["pendingTasks"])
            window.task_input.setText("R52-D04 mouse task")
            QTest.mouseClick(hit, Qt.MouseButton.LeftButton, pos=hit.mapFrom(window, center))
            _process(app)
            assert len(store.load()["pendingTasks"]) - before == 1
            window.task_input.setText("R52-D04 keyboard task")
            button.setFocus()
            QTest.keyClick(button, Qt.Key.Key_Space)
            _process(app)
            assert len(store.load()["pendingTasks"]) - before == 2
            assert "R52-D04 mouse task" in _titles(store)
            assert "R52-D04 keyboard task" in _titles(store)
            font.setPointSizeF(9.0)
            QApplication.setFont(font)
            window.resize(640, 360)
            _process(app)
            window._show_screen(1)
            _process(app)
            _, table_rect, button_rect, center, hit = _button_table_state(window)
            assert table_rect.intersected(button_rect).isEmpty()
            assert hit is window.add_task_button


def test_add_button_wide_2x_no_overlap(app, tmp_path) -> None:
    with _scaled_font(app, 9.0):
        with _tasks_window(app, tmp_path, "en") as (window, store):
            window._show_screen(1)
            window.task_input.setText("R52-D04 wide seed")
            QTest.keyClick(window.task_input, Qt.Key.Key_Return)
            _process(app)
            font = QFont(QApplication.font())
            font.setPointSizeF(18.0)
            QApplication.setFont(font)
            window.resize(1200, 800)
            _process(app)
            window._show_screen(1)
            _process(app)
            _, table_rect, button_rect, center, hit = _button_table_state(window)
            assert table_rect.intersected(button_rect).isEmpty()
            assert hit is window.add_task_button


def test_add_button_geometry_scales(app, tmp_path) -> None:
    with _scaled_font(app, 9.0):
        with _tasks_window(app, tmp_path, "en") as (window, store):
            window._show_screen(1)
            window.task_input.setText("R52-D04 geometry seed")
            QTest.keyClick(window.task_input, Qt.Key.Key_Return)
            _process(app)
            for points in (9.0, 13.5, 18.0):
                font = QFont(QApplication.font())
                font.setPointSizeF(points)
                QApplication.setFont(font)
                window.resize(640, 360)
                _process(app)
                window._show_screen(1)
                _process(app)
                _, _, _, center, hit = _button_table_state(window)
                assert hit is window.add_task_button


def test_add_button_geometry_rtl_2x(app, tmp_path) -> None:
    with _scaled_font(app, 18.0):
        with _tasks_window(app, tmp_path, "ar-XB") as (window, store):
            assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
            window._show_screen(1)
            window.task_input.setText("R52-D04 RTL seed")
            QTest.keyClick(window.task_input, Qt.Key.Key_Return)
            _process(app)
            window.resize(640, 360)
            _process(app)
            window._show_screen(1)
            _process(app)
            button, _, _, center, hit = _button_table_state(window)
            assert button.height() <= 110
            assert hit is button
