"""The tray icon's progress ring shows the current timer's progress.

DSK-08: the ring sits outside the clock face so nothing covers the arc
end, the arc keeps at least 3:1 against its track, an idle tray shows an
empty ring, and the arc ends stay distinct at 0%, 1%, 99% and 100%.
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

from pomodorough.railway_palette import PLATFORM, contrast_ratio
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.tray_progress_icon import ARC_COLOR, TRACK_COLOR
from pomodorough.ui import MainWindow, resource_path

ICON_PIXELS = 64
# Ring geometry in the artwork's 512-unit view box: radius 225, width 56.
TRACK_RADIUS = 225 / 512 * ICON_PIXELS
TRACK = QColor("#111923")
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


def _tray_image(window: MainWindow, size: int = ICON_PIXELS):
    assert window.tray is not None
    return window.tray.icon().pixmap(QSize(size, size)).toImage()


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


# Angles include three and nine o'clock, where the time board used to
# cover the ring, and both sides of each arc end. The arc maps progress
# onto the range between the start stub and the end gap (about 14 to 346
# degrees), so 25% ends near 97 degrees and 75% near 263.
@pytest.mark.parametrize(
    ("progress", "degrees", "expected"),
    [
        (0.2, 40, ARC),
        (0.2, 90, TRACK),
        (0.25, 85, ARC),
        (0.25, 100, TRACK),
        (0.3, 100, ARC),
        (0.5, 160, ARC),
        (0.5, 200, TRACK),
        (0.6, 200, ARC),
        (0.75, 255, ARC),
        (0.75, 275, TRACK),
        (0.8, 270, ARC),
        (0.8, 320, TRACK),
        (1.0, 320, ARC),
        (1.0, 359, ARC),
    ],
)
def test_tray_arc_tracks_timer_progress(window, progress, degrees, expected) -> None:
    window._update_tray_progress(progress, True)

    _assert_color(_ring_pixel(window, degrees), expected)


def test_tray_arc_keeps_non_text_contrast_against_track() -> None:
    assert contrast_ratio(QColor(ARC_COLOR), QColor(TRACK_COLOR)) >= 3.0
    assert contrast_ratio(QColor(ARC_COLOR), QColor(PLATFORM)) >= 3.0


def test_tray_keeps_pointer_and_board_visible(window) -> None:
    window._update_tray_progress(1.0, True)

    # Left time board panel center, and the pointer stem inside the face.
    _assert_color(_tray_pixel(window, 170 / 8, 256 / 8), BOARD)
    _assert_color(_tray_pixel(window, 256 / 8, 120 / 8), POINTER)


def test_idle_tray_shows_an_empty_ring(window) -> None:
    window._update_tray_progress(0.5, True)
    window._update_tray_progress(0.5, False)

    for degrees in (5, 40, 90, 200, 320):
        _assert_color(_ring_pixel(window, degrees), TRACK)


def test_running_timer_at_zero_differs_from_idle(window) -> None:
    window._update_tray_progress(0.0, False)
    idle = _tray_image(window)
    window._update_tray_progress(0.0, True)

    _assert_color(_ring_pixel(window, 2), ARC)
    assert _tray_image(window) != idle


@pytest.mark.parametrize("size", [16, 22, 32])
@pytest.mark.parametrize(
    ("first", "second"), [(0.0, 0.01), (0.99, 1.0), (0.995, 1.0)]
)
def test_neighbouring_percents_render_differently(
    window, first, second, size
) -> None:
    window._update_tray_progress(first, True)
    before = _tray_image(window, size)
    window._update_tray_progress(second, True)

    assert _tray_image(window, size) != before


def test_unfinished_timer_keeps_a_gap_before_twelve(window) -> None:
    window._update_tray_progress(0.999, True)

    _assert_color(_ring_pixel(window, 358), TRACK)


@pytest.mark.parametrize("size", [16, 22, 24, 32])
def test_small_tray_sizes_show_the_arc(window, size) -> None:
    window._update_tray_progress(0.25, True)
    assert window.tray is not None
    image = window.tray.icon().pixmap(QSize(size, size)).toImage()
    radius = 225 / 512 * size
    center = size / 2
    # Three o'clock, just before the arc end at 25%.
    angle = math.radians(80)
    pixel = image.pixelColor(
        round(center + radius * math.sin(angle) - 0.5),
        round(center - radius * math.cos(angle) - 0.5),
    )
    assert pixel.red() > 2 * pixel.blue(), pixel.name()
