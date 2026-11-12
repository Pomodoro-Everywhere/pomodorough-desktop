"""The tray icon's own clock arc shows the current timer's progress.

The artwork ships a static decorative arc. While a timer runs, the tray
must replace it with an arc that grows from twelve o'clock with progress,
painted beneath the pointer and time board like the original.
"""

from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from test_ui import FakeCloud, MemorySecretStore

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow, resource_path

ICON_PIXELS = 64
# Artwork geometry from icon.svg: 512-unit view box, track radius 145.
TRACK_RADIUS = 145 / 512 * ICON_PIXELS
TRACK = QColor("#8fa8b8")
ARC = QColor("#ff604f")
BOARD = QColor("#f5d05b")
POINTER = QColor("#111923")


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, app) -> Iterator[MainWindow]:
    store = Store(
        tmp_path / "tray.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    icon = QIcon(str(resource_path("icon.svg")))
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
        view = MainWindow(store, FakeCloud(authenticated=False), icon, locale="en")
    view.tick_timer.stop()
    view.sync_timer.stop()
    yield view
    view.quitting = True
    view.close()
    view.deleteLater()
    app.processEvents()
    store.close()


def _tray_pixel(window: MainWindow, x: float, y: float) -> QColor:
    assert window.tray is not None
    image = window.tray.icon().pixmap(QSize(ICON_PIXELS, ICON_PIXELS)).toImage()
    return image.pixelColor(round(x), round(y))


def _ring_pixel(window: MainWindow, clockwise_degrees: float) -> QColor:
    """Pixel on the track ring, measured clockwise from twelve o'clock."""
    angle = math.radians(clockwise_degrees)
    center = ICON_PIXELS / 2
    return _tray_pixel(
        window,
        center + TRACK_RADIUS * math.sin(angle),
        center - TRACK_RADIUS * math.cos(angle),
    )


def _assert_color(actual: QColor, expected: QColor) -> None:
    channels = [
        (actual.red(), expected.red()),
        (actual.green(), expected.green()),
        (actual.blue(), expected.blue()),
    ]
    assert all(abs(seen - want) <= 12 for seen, want in channels), (
        actual.name(),
        expected.name(),
    )


# Angles avoid the pointer at twelve o'clock and the board across the
# middle, where the ring is covered by design.
@pytest.mark.parametrize(
    ("progress", "degrees", "expected"),
    [
        (0.0, 40, TRACK),
        (0.0, 200, TRACK),
        (0.0, 320, TRACK),
        (0.2, 40, ARC),
        (0.2, 200, TRACK),
        (0.5, 40, ARC),
        (0.5, 160, ARC),
        (0.5, 200, TRACK),
        (0.6, 200, ARC),
        (0.6, 320, TRACK),
        (1.0, 200, ARC),
        (1.0, 320, ARC),
    ],
)
def test_tray_arc_tracks_timer_progress(window, progress, degrees, expected) -> None:
    window._update_tray_progress(progress, True)

    _assert_color(_ring_pixel(window, degrees), expected)


def test_tray_progress_stays_beneath_pointer_and_board(window) -> None:
    window._update_tray_progress(1.0, True)

    # Left time board panel center, and the pointer stem where it
    # crosses the ring at twelve o'clock.
    _assert_color(_tray_pixel(window, 170 / 8, 256 / 8), BOARD)
    _assert_color(_tray_pixel(window, 256 / 8, 120 / 8), POINTER)


def test_idle_tray_shows_unchanged_app_icon(window) -> None:
    window._update_tray_progress(0.5, True)
    window._update_tray_progress(0.5, False)

    assert window.tray is not None
    assert window.tray.icon().cacheKey() == window.app_icon.cacheKey()
