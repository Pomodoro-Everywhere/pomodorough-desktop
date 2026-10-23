"""R51-D03: settings headings and duration rows stay readable at large fonts.

Qt 6.11.1 verified; allowed 6.8-6.12. Layout-only: no raw WASM envelope
here (covered by D01/D02 envelope proofs); this file asserts Qt geometry.
Scrollbar-max tolerates 1px rounding across offscreen widths/fonts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QSystemTrayIcon
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


def _assert_wrapped_intact(label: QLabel, where: str) -> None:
    panel = label.parentWidget()
    assert label.width() + 1 >= label.minimumSizeHint().width(), where
    assert label.width() <= panel.width() + 1, where
    if label.wordWrap() and label.text():
        assert label.height() + 1 >= label.heightForWidth(label.width()), where


def _assert_settings_intact(window: MainWindow) -> None:
    screen = window.timer_screen
    assert len(screen.phase_buttons) == 3
    for phase, button in screen.phase_buttons.items():
        assert button.width() + 1 >= button.minimumSizeHint().width(), phase
    for index, label in enumerate(screen._settings_heading_labels):
        _assert_wrapped_intact(label, f"heading{index}")
    grid = screen._duration_grid
    for row in range(grid.rowCount()):
        label = grid.itemAtPosition(row, 0).widget()
        spin = grid.itemAtPosition(row, 1).widget()
        _assert_wrapped_intact(label, f"duration-label{row}")
        assert spin.width() + 1 >= spin.minimumSizeHint().width(), f"spin{row}"


def _assert_scrollbar_end_intact(app: QApplication, window: MainWindow) -> None:
    bar = window.timer_screen.settings_scroll.horizontalScrollBar()
    bar.setValue(bar.maximum())
    _process(app, 6)
    _assert_settings_intact(window)
    bar.setValue(bar.minimum())
    _process(app, 6)
    _assert_settings_intact(window)


def _dump(store: Store) -> str:
    return "\n".join(store.connection.iterdump())


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("point_size", [9.0, 18.0])
@pytest.mark.parametrize("size", [(640, 360), (1200, 800)])
def test_headings_and_durations_intact(app, tmp_path, locale, point_size, size):
    with _scaled_font(app, point_size):
        with _main_window(app, tmp_path, locale, *size) as window:
            _assert_settings_intact(window)
            _assert_scrollbar_end_intact(app, window)
            assert not window.timer_screen._settings_reflow_timer.isActive()


def _expected_settings_min(screen) -> int:
    hints = [b.minimumSizeHint().width() for b in screen.phase_buttons.values()]
    margins = screen.right_layout.contentsMargins()
    chrome = margins.left() + margins.right()
    chrome += screen.right_panel.contentsMargins().left()
    chrome += screen.right_panel.contentsMargins().right()
    need_stack = (max(hints) if hints else 0) + chrome
    grid = screen._duration_grid
    grid_spacing = grid.horizontalSpacing()
    if grid_spacing < 0:
        grid_spacing = grid.verticalSpacing()
    if grid_spacing < 0:
        grid_spacing = 0
    row_need = 0
    for row in range(grid.rowCount()):
        label = grid.itemAtPosition(row, 0).widget()
        spin = grid.itemAtPosition(row, 1).widget()
        row_need = max(row_need, label.minimumSizeHint().width() + spin.minimumSizeHint().width())
    duration_need = row_need + grid_spacing if row_need else 0
    child_need = duration_need
    for label in screen._settings_heading_labels:
        child_need = max(child_need, label.minimumSizeHint().width())
    for index in range(screen.right_layout.count()):
        widget = screen.right_layout.itemAt(index).widget()
        if widget is not None:
            child_need = max(child_need, widget.minimumSizeHint().width())
    need_content = child_need + chrome
    return max(need_stack, need_content)


def _expected_scroll_extra(screen) -> int:
    try:
        frame = screen.settings_scroll.frameWidth()
    except RuntimeError:
        frame = 0
    try:
        from PySide6.QtWidgets import QStyle

        extent = screen.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
    except RuntimeError:
        extent = 0
    return frame * 2 + max(0, extent)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_no_spurious_horizontal_scroll_at_base_font(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            screen = window.timer_screen
            expected = _expected_settings_min(screen)
            extra = _expected_scroll_extra(screen)
            assert screen._settings_base_min == expected
            assert screen.right_panel.minimumWidth() == expected
            assert screen.settings_scroll.minimumWidth() == expected + extra
            assert screen.right_panel.minimumWidth() == screen._settings_base_min
            bar = screen.settings_scroll.horizontalScrollBar()
            # Offscreen can expand to host width; font metrics vary by
            # Qt 6.8-6.12. Allow 1px rounding; pre-fix overflow is >>1.
            assert bar.maximum() <= 1


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_live_font_growth_and_shrink_keep_intact(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            before = _dump(window.store)
            _assert_settings_intact(window)
            grown = QFont(QApplication.font())
            grown.setPointSizeF(18.0)
            QApplication.setFont(grown)
            _process(app, 8)
            # No private refresh call; production changeEvent must reflow.
            _assert_settings_intact(window)
            _assert_scrollbar_end_intact(app, window)
            shrunk = QFont(QApplication.font())
            shrunk.setPointSizeF(9.0)
            QApplication.setFont(shrunk)
            _process(app, 8)
            _assert_settings_intact(window)
            assert _dump(window.store) == before


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_duration_keyboard_edit_and_phase_delivery(app, tmp_path, locale):
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path, locale, 640, 360) as window:
            _assert_settings_intact(window)
            screen = window.timer_screen
            spin = screen.duration_spins["focus"]
            spin.setFocus(Qt.FocusReason.ShortcutFocusReason)
            _process(app)
            assert QApplication.focusWidget() is spin
            old = spin.value()
            got: list[tuple[str, int]] = []
            screen.duration_changed.connect(lambda key, value: got.append((key, value)))
            try:
                QTest.keyClick(spin, Qt.Key.Key_Up)
                _process(app)
            finally:
                screen.duration_changed.disconnect()
            assert spin.value() == old + 1
            assert got == [("focus", old + 1)]
            _assert_settings_intact(window)
            delivered: list[str] = []
            screen.phase_selected.connect(delivered.append)
            try:
                button = screen.phase_buttons["short_break"]
                button.setFocus(Qt.FocusReason.ShortcutFocusReason)
                _process(app)
                assert QApplication.focusWidget() is button
                button.click()
                _process(app)
            finally:
                screen.phase_selected.disconnect(delivered.append)
            assert delivered == ["short_break"]
