"""R52-D05: nav buttons reflow without overlap/clip in narrow windows.

Qt 6.x verified; layout-only like R51-D03 (no WASM envelopes here).
Narrow windows with enlarged fonts stack the measured nav group
(row -> 2x2 grid -> column, preserving buttons, group, and signals)
and a frameless scroll area absorbs what the window cannot grant.
Focus and routing survive every transition, including RTL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRect, Qt
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


def _process(app: QApplication, rounds: int = 8) -> None:
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
    _process(app, 12)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _assert_nav_reachable(window: MainWindow) -> None:
    scroll = window._nav_scroll
    viewport = scroll.viewport().rect()
    origin = window.navigation.geometry().topLeft()
    for button in window.navigation.buttons:
        shown = QRect(button.geometry()).translated(origin)
        if not viewport.contains(shown):
            if shown.left() < 0 or shown.right() > viewport.width():
                assert scroll.horizontalScrollBar().maximum() > 0
            if shown.top() < 0 or shown.bottom() > viewport.height():
                assert scroll.verticalScrollBar().maximum() > 0


def _assert_nav_intact(window: MainWindow) -> None:
    buttons = window.navigation.buttons
    assert len(buttons) == 4
    for button in buttons:
        assert button.width() + 1 >= button.minimumSizeHint().width()
        # Checked screen buttons carry a 5px bottom border instead of 2px,
        # so their minimum height runs 3px above siblings; allow it plus
        # 1px rounding like the R51 scrollbar tolerance.
        assert button.height() + 4 >= button.minimumSizeHint().height()
    for first in range(len(buttons)):
        for second in range(first + 1, len(buttons)):
            assert not buttons[first].geometry().intersects(buttons[second].geometry())
    _assert_nav_reachable(window)


def _nav_mode(window: MainWindow) -> str:
    return window.navigation._nav_mode


_CASES = [
    ("en", 9.0, 640, 360, "row"),
    ("en", 9.0, 480, 360, None),
    ("en", 18.0, 640, 360, "grid"),
    ("en", 18.0, 480, 360, "grid"),
    ("en", 18.0, 1200, 800, "row"),
    ("ar-XB", 9.0, 640, 360, None),
    ("ar-XB", 9.0, 1200, 800, "row"),
    ("ar-XB", 18.0, 640, 360, None),
    ("ar-XB", 18.0, 480, 360, "column"),
    ("ar-XB", 18.0, 1200, 800, "row"),
]


@pytest.mark.parametrize("locale,point_size,width,height,mode", _CASES)
def test_modes_and_geometry_intact(app, tmp_path, locale, point_size, width, height, mode):
    with _scaled_font(app, point_size):
        with _main_window(app, tmp_path, locale, width, height) as window:
            _assert_nav_intact(window)
            if mode is not None:
                assert _nav_mode(window) == mode
            else:
                assert _nav_mode(window) in ("row", "grid", "column")
            assert not window._nav_reflow_timer.isActive()


@pytest.mark.parametrize(
    ("locale", "width", "height"), [("en", 640, 360), ("ar-XB", 1200, 800)]
)
def test_no_spurious_bars_and_compact_header_at_base(app, tmp_path, locale, width, height):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, width, height) as window:
            assert _nav_mode(window) == "row"
            assert window._nav_scroll.horizontalScrollBar().maximum() == 0
            assert window._nav_scroll.verticalScrollBar().maximum() == 0
            # The scroll wrapper must not inflate the base header.
            assert window._nav_scroll.height() <= window.settings_button.height() + 2


def test_rtl_mirrors_button_order(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "ar-XB", 1200, 800) as window:
            assert _nav_mode(window) == "row"
            _assert_nav_intact(window)
            xs = [button.geometry().center().x() for button in window.navigation.buttons]
            assert xs == sorted(xs, reverse=True)


@pytest.mark.parametrize("locale,point_size", [("en", 18.0), ("ar-XB", 18.0)])
def test_focus_and_routing_survive_stacked_reflow(app, tmp_path, locale, point_size):
    with _scaled_font(app, point_size):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            _assert_nav_intact(window)
            assert _nav_mode(window) in ("grid", "column")
            buttons = window.navigation.buttons
            for index, button in enumerate(buttons):
                button.setFocus(Qt.FocusReason.ShortcutFocusReason)
                _process(app)
                assert QApplication.focusWidget() is button
                window._reflow_navigation()
                _process(app)
                assert QApplication.focusWidget() is button
                _assert_nav_intact(window)
            delivered: list[int] = []
            window.navigation.screen_requested.connect(delivered.append)
            try:
                for index, button in enumerate(buttons):
                    button.click()
                    _process(app)
                    assert delivered[-1] == index
                    assert window.page_stack.currentIndex() == index
                    assert button.isChecked()
                    assert sum(b.isChecked() for b in buttons) == 1
                    _assert_nav_intact(window)
            finally:
                window.navigation.screen_requested.disconnect(delivered.append)
            for index in range(4):
                # Shortcuts ignore text editing (D75); focus leaves the page.
                buttons[index].setFocus(Qt.FocusReason.ShortcutFocusReason)
                _process(app)
                window._shortcut_show_screen(index)
                _process(app)
                assert window.page_stack.currentIndex() == index
                assert buttons[index].isChecked()
                _assert_nav_intact(window)


def test_widen_restores_row_and_shrink_keeps_intact(app, tmp_path):
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path, "ar-XB", 480, 360) as window:
            assert _nav_mode(window) == "column"
            _assert_nav_intact(window)
            before = "\n".join(window.store.connection.iterdump())
            window.resize(1200, 800)
            _process(app, 12)
            assert _nav_mode(window) == "row"
            _assert_nav_intact(window)
            shrunk = QFont(QApplication.font())
            shrunk.setPointSizeF(9.0)
            QApplication.setFont(shrunk)
            _process(app, 12)
            assert _nav_mode(window) == "row"
            _assert_nav_intact(window)
            assert "\n".join(window.store.connection.iterdump()) == before


def test_live_font_growth_keeps_intact(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "ar-XB", 640, 360) as window:
            _assert_nav_intact(window)
            grown = QFont(QApplication.font())
            grown.setPointSizeF(18.0)
            QApplication.setFont(grown)
            _process(app, 12)
            # No private reflow call; production changeEvent must reflow.
            _assert_nav_intact(window)
            assert not window._nav_reflow_timer.isActive()
