"""R49-D06: runtime application font changes refresh styled text."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
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


def _process(app: QApplication, rounds: int = 4) -> None:
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
def _main_window(app: QApplication, tmp_path, locale: str = "en"):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    cloud = FakeCloud(authenticated=True)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, cloud, QIcon(), locale=locale)
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


def test_runtime_font_change_refreshes_sheet_and_geometry(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path) as window:
            assert "font-size: 9.0pt" in _root_block(window.styleSheet())
            before_ls = window.primary_button.fontMetrics().lineSpacing()
            # Qt font metrics differ by platform (macOS 17, Ubuntu 16 for 9pt). Keep relative intent.
            assert 15 <= before_ls <= 19
            before_row = window.task_table.rowHeight(0)
            before_invite = window.invite_input.minimumHeight()
            scaled = QFont(QApplication.font())
            scaled.setPointSizeF(18.0)
            QApplication.setFont(scaled)
            _process(app, 8)
            # No private refresh call here; production changeEvent must act.
            assert "font-size: 18.0pt" in _root_block(window.styleSheet())
            assert 30 <= window.primary_button.fontMetrics().lineSpacing() <= 36
            assert window.primary_button.fontMetrics().lineSpacing() > before_ls
            assert window.task_table.rowHeight(0) > before_row
            assert window.invite_input.minimumHeight() > before_invite


def test_palette_only_preserves_font_scale(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path) as window:
            assert "font-size: 9.0pt" in _root_block(window.styleSheet())
            before_ls = window.primary_button.fontMetrics().lineSpacing()
            before_row = window.task_table.rowHeight(0)
            palette = QApplication.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor("#123456"))
            QApplication.setPalette(palette)
            _process(app, 6)
            try:
                assert "font-size: 9.0pt" in _root_block(window.styleSheet())
                assert window.primary_button.fontMetrics().lineSpacing() == before_ls
                assert window.task_table.rowHeight(0) == before_row
            finally:
                QApplication.setPalette(QApplication.style().standardPalette())
                _process(app)


def test_unchanged_font_preserves_geometry(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path) as window:
            assert "font-size: 9.0pt" in _root_block(window.styleSheet())
            before_ls = window.primary_button.fontMetrics().lineSpacing()
            before_row = window.task_table.rowHeight(0)
            before_invite = window.invite_input.minimumHeight()
            same = QFont(QApplication.font())
            same.setPointSizeF(9.0)
            QApplication.setFont(same)
            _process(app, 6)
            assert "font-size: 9.0pt" in _root_block(window.styleSheet())
            assert "font-size: 18.0pt" not in _root_block(window.styleSheet())
            assert window.primary_button.fontMetrics().lineSpacing() == before_ls
            assert window.task_table.rowHeight(0) == before_row
            assert window.invite_input.minimumHeight() == before_invite
