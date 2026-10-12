"""R48-D04: fixed-pixel text derives from the application font."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon

from pomodorough.network_screen import NetworkScreen
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from pomodorough.ui_views import MainWindowViewMixin
from test_ui import FakeCloud, MemorySecretStore

KEY_CONTROLS = (
    "replication_mode_combo",
    "privacy_policy_button",
    "delete_account_button",
    "room_name_input",
    "create_room_button",
    "invite_input",
    "join_room_button",
    "invite_output",
    "copy_invite_button",
    "refresh_invite_button",
    "sync_iroh_button",
    "leave_room_button",
    "network_details",
)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication) -> None:
    app.processEvents()
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


def _window_overlaps(window: QWidget, screen) -> list[tuple[str, str]]:
    found = []
    for i in range(len(KEY_CONTROLS)):
        for j in range(i + 1, len(KEY_CONTROLS)):
            first = getattr(screen, KEY_CONTROLS[i])
            second = getattr(screen, KEY_CONTROLS[j])
            if not first.isVisible() or not second.isVisible():
                continue
            if first.isAncestorOf(second) or second.isAncestorOf(first):
                continue
            first_rect = QRect(first.mapTo(window, QPoint(0, 0)), first.size())
            second_rect = QRect(second.mapTo(window, QPoint(0, 0)), second.size())
            if first_rect.intersects(second_rect):
                inter = first_rect.intersected(second_rect)
                if inter.width() > 0 and inter.height() > 0:
                    found.append((KEY_CONTROLS[i], KEY_CONTROLS[j]))
    return found


def _assert_focus_reaches(app: QApplication, screen) -> None:
    scroll = getattr(screen, "_scroll", None)
    for name in KEY_CONTROLS:
        widget = getattr(screen, name, None)
        if widget is None or not widget.isEnabled() or not widget.isVisible():
            continue
        if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        if scroll is not None:
            scroll.ensureWidgetVisible(widget)
            _process(app)
        widget.setFocus()
        _process(app)
        assert widget.hasFocus(), name
        if scroll is not None:
            scroll.ensureWidgetVisible(widget)
            _process(app)
        assert not widget.visibleRegion().isEmpty(), name


@contextmanager
def _main_window(app: QApplication, tmp_path, locale: str, width: int, height: int):
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
    window._display_screen(3)
    window.resize(width, height)
    window.show()
    _process(app)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def test_stylesheet_scales_and_keeps_hierarchy(app):
    with _scaled_font(app, 9.0):
        base = MainWindowViewMixin._stylesheet()
        base_net = NetworkScreen.stylesheet()
    with _scaled_font(app, 13.5):
        mid = MainWindowViewMixin._stylesheet()
        mid_net = NetworkScreen.stylesheet()
    with _scaled_font(app, 18.0):
        large = MainWindowViewMixin._stylesheet()
        large_net = NetworkScreen.stylesheet()
    assert "font-size: 9.0pt" in base
    assert "font-size: 13.5pt" in mid
    assert "font-size: 18.0pt" in large
    assert "12px" not in base and "24px" not in base and "font-size: 9px" not in base
    assert "font-size: 9px" not in base_net
    for sheet in (base, mid, large):
        brand = sheet.split("QLabel#brand")[1].split("}")[0]
        section = sheet.split("QLabel#sectionTitle")[1].split("}")[0]
        assert "18.0pt" in brand or "27.0pt" in brand or "36.0pt" in brand
        assert "10.5pt" in section or "15.8pt" in section or "21.0pt" in section
    assert "min-height: 29px" in base
    assert "min-height: 44px" in mid
    assert "min-height: 58px" in large
    assert "6.8pt" in base or "6.7pt" in base
    assert "10.1pt" in mid or "10.2pt" in mid
    assert "10.1pt" in mid_net
    assert "13.5pt" in large_net


def test_mainwindow_1x_metrics_wrap_scroll(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            screen = window.network_screen
            assert screen._scroll.widgetResizable()
            assert window.primary_button.fontMetrics().lineSpacing() >= 12
            title_ls = screen.findChild(QWidget).fontMetrics().lineSpacing()
            assert title_ls >= 12
            assert screen.network_details.wordWrap()
            for name in ("network_unavailable", "iroh_first_room_guidance"):
                assert getattr(screen, name).wordWrap()
            assert _window_overlaps(window, screen) == []
            assert not screen.replication_mode_combo.visibleRegion().isEmpty()
            _assert_focus_reaches(app, screen)
            screen._scroll.verticalScrollBar().setValue(
                screen._scroll.verticalScrollBar().maximum()
            )
            _process(app)
            assert not screen.network_details.visibleRegion().isEmpty()


def test_mainwindow_1_5x_grows_and_scrolls(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "en", 640, 360) as base_window:
            base_ls = base_window.primary_button.fontMetrics().lineSpacing()
            base_btn_h = base_window.primary_button.sizeHint().height()
    with _scaled_font(app, 13.5):
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            screen = window.network_screen
            assert window.primary_button.fontMetrics().lineSpacing() > base_ls
            assert window.primary_button.sizeHint().height() >= base_btn_h
            assert window.primary_button.minimumHeight() > 36
            assert screen.network_details.wordWrap()
            assert _window_overlaps(window, screen) == []
            assert screen._scroll.verticalScrollBar().maximum() > 0
            _assert_focus_reaches(app, screen)


def test_mainwindow_2x_grows_and_scrolls(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "en", 640, 360) as base_window:
            base_ls = base_window.primary_button.fontMetrics().lineSpacing()
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            screen = window.network_screen
            assert window.primary_button.fontMetrics().lineSpacing() > base_ls
            assert window.primary_button.minimumHeight() > 44
            micro_ls = screen.replication_mode_combo.fontMetrics().lineSpacing()
            assert micro_ls >= base_ls
            assert screen.network_details.wordWrap()
            assert _window_overlaps(window, screen) == []
            assert screen._scroll.verticalScrollBar().maximum() > 0
            _assert_focus_reaches(app, screen)
            screen._scroll.verticalScrollBar().setValue(
                screen._scroll.verticalScrollBar().maximum()
            )
            _process(app)
            assert not screen.network_details.visibleRegion().isEmpty()


def test_rtl_1_5x_no_overlap(app, tmp_path):
    with _scaled_font(app, 13.5):
        with _main_window(app, tmp_path, "ar-XB", 640, 360) as window:
            assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
            screen = window.network_screen
            assert _window_overlaps(window, screen) == []
            assert screen.network_details.wordWrap()
            _assert_focus_reaches(app, screen)


def test_enabled_room_2x_reachable(app, tmp_path):
    with _scaled_font(app, 18.0):
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            screen = window.network_screen
            screen.render(
                replication_mode="iroh",
                iroh_status="SYNC READY",
                iroh_details={"peerCount": 2, "operationCount": 5},
                invite="TICKET-abc",
                room={"roomId": "abcdef1234567890", "roomName": "Room"},
                available=True,
                unavailable_reason="",
                cloud_authenticated=True,
                cloud_deleting_account=False,
            )
            _process(app)
            assert screen.refresh_invite_button.isEnabled()
            assert screen.sync_iroh_button.isEnabled()
            assert screen.leave_room_button.isEnabled()
            assert _window_overlaps(window, screen) == []
            _assert_focus_reaches(app, screen)


def test_font_refresh_updates_live_window(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            before = window.styleSheet()
            assert "9.0pt" in before
            scaled = QFont(QApplication.font())
            scaled.setPointSizeF(18.0)
            QApplication.setFont(scaled)
            _process(app)
            window._refresh_stylesheet()
            _process(app)
            assert "18.0pt" in window.styleSheet()
