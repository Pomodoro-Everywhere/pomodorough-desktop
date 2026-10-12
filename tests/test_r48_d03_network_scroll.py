"""R48-D03: Network page scrolls without overlap at small sizes."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget, QVBoxLayout, QSystemTrayIcon
from unittest.mock import patch

from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
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


def _content_overlaps(screen: NetworkScreen) -> list[tuple[str, str]]:
    found = []
    controls = [n for n in KEY_CONTROLS if hasattr(screen, n)]
    for i in range(len(controls)):
        for j in range(i + 1, len(controls)):
            first = getattr(screen, controls[i])
            second = getattr(screen, controls[j])
            if not first.isVisible() or not second.isVisible():
                continue
            if first.isAncestorOf(second) or second.isAncestorOf(first):
                continue
            first_pos = first.mapTo(screen._content, QPoint(0, 0))
            second_pos = second.mapTo(screen._content, QPoint(0, 0))
            first_rect = QRect(first_pos, first.size())
            second_rect = QRect(second_pos, second.size())
            if first_rect.intersects(second_rect):
                inter = first_rect.intersected(second_rect)
                if inter.width() > 0 and inter.height() > 0:
                    found.append((controls[i], controls[j]))
    return found


def _window_overlaps(window: QWidget, screen: NetworkScreen) -> list[tuple[str, str]]:
    found = []
    controls = [n for n in KEY_CONTROLS if hasattr(screen, n)]
    for i in range(len(controls)):
        for j in range(i + 1, len(controls)):
            first = getattr(screen, controls[i])
            second = getattr(screen, controls[j])
            if not first.isVisible() or not second.isVisible():
                continue
            if first.isAncestorOf(second) or second.isAncestorOf(first):
                continue
            first_rect = QRect(first.mapTo(window, QPoint(0, 0)), first.size())
            second_rect = QRect(second.mapTo(window, QPoint(0, 0)), second.size())
            if first_rect.intersects(second_rect):
                inter = first_rect.intersected(second_rect)
                if inter.width() > 0 and inter.height() > 0:
                    found.append((controls[i], controls[j]))
    return found


def _assert_scrollable(screen: NetworkScreen) -> None:
    assert hasattr(screen, "_scroll")
    assert screen._scroll.widgetResizable()
    assert screen._scroll.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    content_hint = screen._content.sizeHint().height()
    viewport = screen._scroll.viewport().height()
    if content_hint > viewport:
        assert screen._scroll.verticalScrollBar().maximum() > 0


def _assert_focus_reaches(app: QApplication, screen: NetworkScreen) -> None:
    for name in KEY_CONTROLS:
        widget = getattr(screen, name, None)
        if widget is None or not widget.isEnabled() or not widget.isVisible():
            continue
        if widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
            continue
        widget.setFocus()
        _process(app)
        assert widget.hasFocus(), name
        assert not widget.visibleRegion().isEmpty(), name


@contextmanager
def _main_window(app: QApplication, tmp_path, locale: str, width: int, height: int):
    store = Store(tmp_path / "state.sqlite3", shared_core=SharedCore(), iroh_secret_store=MemorySecretStore())
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


@contextmanager
def _standalone(app: QApplication, locale: str, width: int, height: int, **render_kwargs):
    screen = NetworkScreen(Strings(locale), "iroh")
    defaults = {
        "replication_mode": "iroh", "iroh_status": "SYNC READY", "iroh_details": {},
        "invite": "", "room": None, "available": True, "unavailable_reason": "",
        "cloud_authenticated": True, "cloud_deleting_account": False,
    }
    defaults.update(render_kwargs)
    screen.render(**defaults)
    window = QWidget()
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(screen)
    if Strings(locale).is_rtl:
        window.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        screen.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    window.resize(width, height)
    window.show()
    _process(app)
    try:
        yield screen, window
    finally:
        window.close()
        window.deleteLater()
        screen.deleteLater()
        _process(app)


def test_default_640x360_has_scroll_and_no_overlap(app, tmp_path):
    with _main_window(app, tmp_path, "en", 640, 360) as window:
        screen = window.network_screen
        _assert_scrollable(screen)
        assert _content_overlaps(screen) == []
        assert _window_overlaps(window, screen) == []
        assert not screen.replication_mode_combo.visibleRegion().isEmpty()
        screen._scroll.verticalScrollBar().setValue(screen._scroll.verticalScrollBar().maximum())
        _process(app)
        assert not screen.network_details.visibleRegion().isEmpty()


def test_minimum_600x340_no_overlap(app, tmp_path):
    with _main_window(app, tmp_path, "en", 600, 340) as window:
        screen = window.network_screen
        _assert_scrollable(screen)
        assert _content_overlaps(screen) == []
        assert _window_overlaps(window, screen) == []
        _assert_focus_reaches(app, screen)


def test_short_viewport_scrolls_without_overlap(app):
    with _standalone(app, "en", 640, 280) as (screen, window):
        _assert_scrollable(screen)
        assert _content_overlaps(screen) == []
        assert _window_overlaps(window, screen) == []
        assert screen._scroll.verticalScrollBar().maximum() > 0


def test_rtl_no_overlap_and_direction(app, tmp_path):
    with _main_window(app, tmp_path, "ar-XB", 640, 360) as window:
        assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
        screen = window.network_screen
        _assert_scrollable(screen)
        assert _content_overlaps(screen) == []
        assert _window_overlaps(window, screen) == []
        _assert_focus_reaches(app, screen)


def test_large_font_wraps_and_focus_scrolls(app):
    previous = QApplication.font()
    large = QFont(previous)
    large.setPointSize(20)
    QApplication.setFont(large)
    try:
        with _standalone(app, "en", 640, 360) as (screen, window):
            assert screen.network_details.wordWrap()
            _assert_scrollable(screen)
            assert _content_overlaps(screen) == []
            assert _window_overlaps(window, screen) == []
            assert screen._scroll.verticalScrollBar().maximum() > 0
            _assert_focus_reaches(app, screen)
    finally:
        QApplication.setFont(previous)


def test_enabled_no_room_create_join_reachable(app):
    with _standalone(app, "en", 640, 360, room=None, available=True) as (screen, _window):
        assert screen.create_room_button.isEnabled()
        assert screen.join_room_button.isEnabled()
        assert _content_overlaps(screen) == []
        _assert_focus_reaches(app, screen)


def test_enabled_room_refresh_sync_leave_reachable(app):
    room = {"roomId": "abcdef1234567890", "roomName": "Room", "peerCount": 2, "operationCount": 5}
    details = {"peerCount": 2, "operationCount": 5}
    kwargs = {"room": room, "iroh_details": details, "available": True, "invite": "TICKET-abc"}
    with _standalone(app, "en", 640, 360, **kwargs) as (screen, _window):
        assert screen.refresh_invite_button.isEnabled()
        assert screen.sync_iroh_button.isEnabled()
        assert screen.leave_room_button.isEnabled()
        assert screen.copy_invite_button.isEnabled()
        assert _content_overlaps(screen) == []
        _assert_focus_reaches(app, screen)


def test_resize_keeps_no_overlap(app, tmp_path):
    with _main_window(app, tmp_path, "en", 640, 360) as window:
        screen = window.network_screen
        assert _window_overlaps(window, screen) == []
        window.resize(1024, 768)
        _process(app)
        assert _content_overlaps(screen) == []
        assert _window_overlaps(window, screen) == []
        _assert_focus_reaches(app, screen)


def test_invite_fields_stay_growable(app):
    with _standalone(app, "en", 640, 360) as (screen, _window):
        for field in (screen.invite_input, screen.invite_output):
            assert field.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
            assert field.maximumHeight() == 16777215
            assert field.minimumHeight() >= field.fontMetrics().lineSpacing()
