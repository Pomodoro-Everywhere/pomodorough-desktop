"""DSK-01: every navigation destination stays in view at reachable sizes.

macOS opens with a 13 pt system font where Fusion uses 9 pt. The style
sheet scales with the font, so the four destinations stopped fitting
one row at the default window size. The header fell back to the 2x2
grid, the page squeezed the header to one row, and ARRIVALS and NETWORK
sat behind a scroll stepper with no cue.

UX round 2 B1 replaced the stacked fallbacks: a row that does not fit
collapses into one menu button that offers all four destinations. The
tests assert every destination through the row, or through the menu
button and its menu.

The tests set the application font instead of relying on the host
platform font, and assert visibility by geometry only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
)

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from pomodorough.ui_views import MainWindowViewMixin
from test_r52_d05_nav_reflow import (
    _assert_nav_intact,
    _process,
    _scaled_font,
    _shown_nav_controls,
)
from test_ui import FakeCloud, MemorySecretStore

# 13 pt is the macOS system font. 18 pt and 27 pt are 2x and 3x Fusion.
_POINT_SIZES = [13.0, 18.0, 27.0]
_LOCALES = ["en", "ar-XB"]


class _ControlTypeSpacedStyle(QProxyStyle):
    """Spaces layouts by control type only, as the macOS style does."""

    GAP = 14

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (
            QStyle.PixelMetric.PM_LayoutHorizontalSpacing,
            QStyle.PixelMetric.PM_LayoutVerticalSpacing,
        ):
            return -1
        return super().pixelMetric(metric, option, widget)

    def layoutSpacing(self, control1, control2, orientation, option=None, widget=None):
        return self.GAP


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _default_window(
    app: QApplication, tmp_path: Path, locale: str, authenticated: bool = False
):
    """Open the real window at the size it chooses for itself."""
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    cloud = FakeCloud(authenticated=authenticated)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, cloud, QIcon(), locale=locale)
    window.tick_timer.stop()
    window.sync_timer.stop()
    window.show()
    _process(app, 20)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _hit_points(button: QPushButton) -> list[QPoint]:
    rect = button.rect()
    return [
        rect.center(),
        rect.topLeft(),
        rect.topRight(),
        rect.bottomLeft(),
        rect.bottomRight(),
    ]


def _hidden_destinations(window: MainWindow) -> list[str]:
    """Destinations not fully painted, or covered, or outside the window.

    A collapsed navigation shows its destinations through the menu button,
    so the menu button is the control that must be whole.
    """
    hidden = []
    for button in _shown_nav_controls(window):
        in_window = QRect(button.mapTo(window, button.rect().topLeft()), button.size())
        fully_painted = button.visibleRegion().boundingRect() == button.rect()
        uncovered = all(
            window.childAt(button.mapTo(window, point)) is button
            for point in _hit_points(button)
        )
        if not (fully_painted and uncovered and window.rect().contains(in_window)):
            hidden.append(button.text())
    return hidden


def _assert_every_destination_shown(window: MainWindow) -> None:
    assert _hidden_destinations(window) == []
    navigation = window.navigation
    if navigation._nav_mode == "menu":
        assert [action.text() for action in navigation.destination_actions] == [
            button.text() for button in navigation.buttons
        ]
        assert all(action.isEnabled() for action in navigation.destination_actions)
    assert window._nav_scroll.verticalScrollBar().maximum() == 0
    assert window._nav_scroll.horizontalScrollBar().maximum() == 0
    for action in (window.settings_button, window.account_button):
        if action.isVisible():
            in_window = QRect(action.mapTo(window, QPoint(0, 0)), action.size())
            assert window.rect().contains(in_window)


def _assert_every_destination_in_view(window: MainWindow) -> None:
    _assert_every_destination_shown(window)
    _assert_nav_intact(window)


def _shrink_to_narrowest_row(app: QApplication, window: MainWindow) -> None:
    """Resize to the narrowest width that still fits the one-row navigation."""
    stacked, row = window.minimumWidth() - 1, window.width()
    while not _fits_one_row(app, window, row):
        stacked, row = row, 2 * row
    while row - stacked > 1:
        middle = (stacked + row) // 2
        if _fits_one_row(app, window, middle):
            row = middle
        else:
            stacked = middle
    assert _fits_one_row(app, window, row)


def _fits_one_row(app: QApplication, window: MainWindow, width: int) -> bool:
    window.resize(width, window.height())
    _process(app, 20)
    return window.navigation._nav_mode == "row"


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("points", _POINT_SIZES)
def test_default_window_shows_every_destination(app, tmp_path, locale, points):
    with _scaled_font(app, points):
        with _default_window(app, tmp_path, locale) as window:
            _assert_every_destination_in_view(window)
            screen = QApplication.primaryScreen().availableGeometry()
            assert window.width() <= screen.width()
            assert window.height() <= screen.height()
            if window.width() < screen.width():
                # Only a screen too narrow for one row collapses the default.
                assert window.navigation._nav_mode == "row"


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("points", _POINT_SIZES)
def test_minimum_window_shows_every_destination(app, tmp_path, locale, points):
    with _scaled_font(app, points):
        with _default_window(app, tmp_path, locale) as window:
            window.resize(window.minimumSize())
            _process(app, 20)
            assert window.size() == window.minimumSize()
            _assert_every_destination_shown(window)
            _need_row, need_menu = window.navigation.measured_nav_needs()
            if window._header_nav_avail() >= need_menu:
                # The fixed minimum width is narrower than the widest label
                # at 3x pseudo-locale text. B1 then shortens the menu label
                # with an ellipsis; the full name stays in the tooltip.
                _assert_nav_intact(window)


@pytest.mark.parametrize("points", _POINT_SIZES)
def test_every_destination_activates_its_page_by_pointer(app, tmp_path, points):
    with _scaled_font(app, points):
        with _default_window(app, tmp_path, "en") as window:
            navigation = window.navigation
            for index, button in enumerate(navigation.buttons):
                collapsed = navigation._nav_mode == "menu"
                shown = navigation.menu_button if collapsed else button
                point = shown.mapTo(window, shown.rect().center())
                target = window.childAt(point)
                assert target is shown
                if shown is navigation.menu_button:
                    # A click opens the menu modally; choosing a destination
                    # in it triggers the action, as done here.
                    navigation.destination_actions[index].trigger()
                else:
                    QTest.mouseClick(target, Qt.MouseButton.LeftButton)
                _process(app)
                assert window.page_stack.currentIndex() == index
                _assert_every_destination_in_view(window)


def test_showing_the_settings_action_restacks_a_row_that_only_just_fit(app, tmp_path):
    # Tasks hides the settings action, so a row can fit there at a width
    # where Timer has no room for it.
    with _scaled_font(app, 13.0):
        with _default_window(app, tmp_path, "en") as window:
            tasks, timer = window.navigation.buttons[1], window.navigation.buttons[0]
            QTest.mouseClick(tasks, Qt.MouseButton.LeftButton)
            _process(app, 20)
            assert not window.settings_button.isVisible()
            _shrink_to_narrowest_row(app, window)
            _assert_every_destination_in_view(window)
            QTest.mouseClick(timer, Qt.MouseButton.LeftButton)
            _process(app, 20)
            assert window.settings_button.isVisible()
            assert window.navigation._nav_mode != "row"
            _assert_every_destination_in_view(window)


def test_wider_account_label_restacks_a_row_that_only_just_fit(app, tmp_path):
    # Signing out swaps a one-glyph account label for the sign-in label.
    with _scaled_font(app, 13.0):
        with _default_window(app, tmp_path, "en", authenticated=True) as window:
            window._set_account_state(True)
            signed_in_label = window.account_button.text()
            _shrink_to_narrowest_row(app, window)
            _assert_every_destination_in_view(window)
            window.cloud.authenticated = False
            window._set_account_state(False)
            _process(app, 20)
            assert window.account_button.text() != signed_in_label
            assert window.navigation._nav_mode != "row"
            _assert_every_destination_in_view(window)


def test_control_type_spacing_keeps_a_row_that_only_just_fits_whole(app, tmp_path):
    # The macOS style has no single layout spacing; the navigation grid
    # then spaces its cells by control type. A stand-in style does the
    # same here, so the test does not depend on the host platform.
    with _scaled_font(app, 13.0):
        with _default_window(app, tmp_path, "en") as window:
            style = _ControlTypeSpacedStyle()
            style.setParent(window)
            window.navigation.setStyle(style)
            assert window.navigation._nav_layout.horizontalSpacing() < 0
            # A resize makes the header measure again under the new style.
            window.resize(window.width() + 1, window.height())
            _process(app, 20)
            _shrink_to_narrowest_row(app, window)
            buttons = window.navigation.buttons
            assert buttons[1].x() - buttons[0].geometry().right() - 1 == style.GAP
            _assert_every_destination_in_view(window)


def test_narrow_screen_keeps_default_window_on_screen(app, tmp_path):
    # A screen too narrow for one row caps the default width. The menu
    # button then offers every destination instead of scrolling.
    with _scaled_font(app, 13.0):
        with _default_window(app, tmp_path, "en") as probe:
            assert probe.navigation._nav_mode == "row"
            narrow_screen = probe.width() - 1
        with patch.object(
            MainWindowViewMixin, "_available_screen_width", return_value=narrow_screen
        ):
            with _default_window(app, tmp_path / "narrow", "en") as window:
                assert window.width() <= narrow_screen
                assert window.navigation._nav_mode != "row"
                _assert_every_destination_in_view(window)


def test_short_screen_keeps_scroll_recovery(app, tmp_path):
    # A stacked navigation once raised the window minimum toward the screen
    # height. B1 collapses instead, so a large font leaves the minimum
    # height where the base font put it, and no screen can be overrun.
    with _scaled_font(app, 9.0):
        with _default_window(app, tmp_path, "en") as probe:
            base_minimum = probe.minimumHeight()
    with _scaled_font(app, 27.0):
        with _default_window(app, tmp_path / "short", "en") as window:
            assert window.minimumHeight() == base_minimum
            assert window._nav_scroll.verticalScrollBar().maximum() == 0
            _assert_nav_intact(window)


def test_window_minimum_follows_live_font_changes(app, tmp_path):
    with _scaled_font(app, 9.0):
        with _default_window(app, tmp_path, "en") as window:
            one_row_minimum = window.minimumHeight()
            font = QFont(QApplication.font())
            font.setPointSizeF(27.0)
            QApplication.setFont(font)
            _process(app, 25)
            navigation = window.navigation
            assert navigation._nav_mode == "menu"
            # B1: the collapsed navigation is one control tall and the
            # window minimum does not grow.
            one_control = navigation.menu_button.minimumSizeHint().height()
            assert navigation.minimumSizeHint().height() == one_control
            assert window.minimumHeight() == one_row_minimum
            _assert_every_destination_in_view(window)
            font.setPointSizeF(9.0)
            QApplication.setFont(font)
            _process(app, 25)
            assert navigation._nav_mode == "row"
            assert window.minimumHeight() == one_row_minimum
            _assert_every_destination_in_view(window)
