"""R49-D06 residual: palette/font sequences and synthetic font-change path."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from unittest.mock import patch

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication, rounds: int = 6) -> None:
    for _ in range(rounds):
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


@contextmanager
def _main_window(app: QApplication, tmp_path):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    cloud = FakeCloud(authenticated=True)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, cloud, QIcon(), locale="en")
    window.tick_timer.stop()
    window.sync_timer.stop()
    tasks = [{"id": f"task-{i}", "title": f"Task {i}"} for i in range(3)]
    window.tasks = tasks
    window.tasks_screen.render(tasks, [], mutations_enabled=True)
    window.resize(640, 360)
    window.show()
    _process(app, 6)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _root_block(sheet: str) -> str:
    for part in sheet.split("}"):
        if "QWidget#root" in part:
            return part
    return ""


def test_font_then_palette_preserves_new_scale(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path) as window:
            scaled = QFont(QApplication.font())
            scaled.setPointSizeF(18.0)
            QApplication.setFont(scaled)
            _process(app, 8)
            assert "font-size: 18.0pt" in _root_block(window.styleSheet())
            palette = QApplication.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor("#223344"))
            QApplication.setPalette(palette)
            _process(app, 6)
            try:
                assert "font-size: 18.0pt" in _root_block(window.styleSheet())
                assert 30 <= window.primary_button.fontMetrics().lineSpacing() <= 36
            finally:
                QApplication.setPalette(QApplication.style().standardPalette())
                _process(app)


def test_palette_then_font_refreshes_to_new_scale(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path) as window:
            palette = QApplication.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor("#334455"))
            QApplication.setPalette(palette)
            _process(app, 6)
            try:
                assert "font-size: 9.0pt" in _root_block(window.styleSheet())
                scaled = QFont(QApplication.font())
                scaled.setPointSizeF(18.0)
                QApplication.setFont(scaled)
                _process(app, 8)
                assert "font-size: 18.0pt" in _root_block(window.styleSheet())
                assert window.task_table.rowHeight(0) > 40
            finally:
                QApplication.setPalette(QApplication.style().standardPalette())
                _process(app)


def test_synthetic_application_font_change_refreshes_sheet(app, tmp_path):
    import PySide6
    if tuple(int(x) for x in PySide6.__version__.split(".")[:2]) >= (6, 12):
        pytest.skip("Synthetic ApplicationFontChange aborts on PySide 6.12+; real setFont path still covered")
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path) as window:
            assert "font-size: 18.0pt" in _root_block(window.styleSheet())
            base = QFont(QApplication.font())
            base.setPointSizeF(9.0)
            QApplication.setFont(base)
            _process(app, 4)
            QApplication.sendEvent(
                window, QEvent(QEvent.Type.ApplicationFontChange)
            )
            _process(app, 6)
            assert "font-size: 9.0pt" in _root_block(window.styleSheet())
            # Qt font metrics differ by platform (macOS 17, Ubuntu 16 for 9pt).
            assert 15 <= window.primary_button.fontMetrics().lineSpacing() <= 19
