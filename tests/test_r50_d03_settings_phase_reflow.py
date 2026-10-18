"""R50-D03: settings phase choices keep intact labels through reflow."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
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
def _main_window(app: QApplication, tmp_path: Path, locale: str, width: int, height: int):
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
    window.resize(width, height)
    window.show()
    _process(app)
    window.settings_button.setChecked(True)
    _process(app, 8)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _assert_phase_intact(window: MainWindow) -> None:
    assert len(window.timer_screen.duration_spins) == 3
    for phase, button in window.timer_screen.phase_buttons.items():
        assert button.width() + 1 >= button.minimumSizeHint().width(), phase


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("point_size", [9.0, 18.0])
@pytest.mark.parametrize("size", [(640, 360), (1200, 800)])
def test_phase_widths_meet_hints(app, tmp_path, locale, point_size, size):
    with _scaled_font(app, point_size):
        with _main_window(app, tmp_path, locale, *size) as window:
            _assert_phase_intact(window)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_live_font_growth_and_shrink_keep_intact(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            _assert_phase_intact(window)
            grown = QFont(QApplication.font())
            grown.setPointSizeF(18.0)
            QApplication.setFont(grown)
            _process(app, 8)
            # No private refresh call; production changeEvent must reflow.
            _assert_phase_intact(window)
            shrunk = QFont(QApplication.font())
            shrunk.setPointSizeF(9.0)
            QApplication.setFont(shrunk)
            _process(app, 8)
            _assert_phase_intact(window)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_focus_and_phase_delivery_after_reflow(app, tmp_path, locale):
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            _assert_phase_intact(window)
            for phase, button in window.timer_screen.phase_buttons.items():
                button.setFocus(Qt.FocusReason.ShortcutFocusReason)
                _process(app)
                assert QApplication.focusWidget() is button
                got: list[str] = []
                window.timer_screen.phase_selected.connect(got.append)
                try:
                    button.click()
                    _process(app)
                finally:
                    window.timer_screen.phase_selected.disconnect(got.append)
                assert got == [phase]
