"""UX round 2 batch C M3: the Timer primary stays whole at large fonts.

ROUND2-SPEC B1 asks for the primary control and the countdown fully
visible from 9 to 30 pt at the default and the minimum window. At large
fonts the Timer controls column used to ask for the one-line width of its
widest button, which was wider than the page, so the primary button was
cut off at the page edge. The column width now comes from the measured page
width, and a button that cannot keep its label on one line wraps it.

Every expectation is measured from the widgets; no pixel constant is used.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QSystemTrayIcon,
    QWidget,
)
from test_r52_d05_nav_reflow import _process, _scaled_font
from test_ui import FakeCloud, MemorySecretStore
from test_ux_timer_controls import SETTINGS, _render

from pomodorough.localization import Strings
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.timer_screen import TimerScreen
from pomodorough.ui import MainWindow

POINT_SIZES = (9.0, 18.0, 20.0, 24.0, 27.0, 30.0)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _timer_window(app: QApplication, tmp_path: Path, locale: str, size: str):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, FakeCloud(), QIcon(), locale=locale)
    window.tick_timer.stop()
    window.sync_timer.stop()
    window.show()
    _process(app, 12)
    try:
        if size == "minimum":
            window.resize(window.minimumSize())
            _process(app, 12)
            assert window.size() == window.minimumSize()
        window.navigation.screen_requested.emit(0)
        _process(app, 12)
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _rect_in(widget: QWidget, ancestor: QWidget) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _assert_whole(widget: QWidget, window: QWidget) -> None:
    """Painted in full, uncovered at the centre and corners, inside the window."""
    assert widget.isVisible()
    assert widget.visibleRegion().boundingRect() == widget.rect()
    assert window.rect().contains(_rect_in(widget, window)), (
        _rect_in(widget, window),
        window.rect(),
    )
    rect = widget.rect()
    for point in (rect.center(), rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()):
        hit = window.childAt(widget.mapTo(window, point))
        assert hit is widget or widget.isAncestorOf(hit), (point, hit)


def _assert_label_room(button: QPushButton) -> None:
    """The button is at least as large as its label needs at its width."""
    assert button.width() >= button.minimumSizeHint().width()
    need = button.minimumSizeHint().height()
    if button.hasHeightForWidth():
        need = button.heightForWidth(button.width())
    assert button.height() >= need


@pytest.mark.parametrize("size", ["default", "minimum"])
@pytest.mark.parametrize("points", POINT_SIZES)
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_timer_primary_and_clock_are_whole(app, tmp_path, locale, points, size):
    with _scaled_font(app, points):
        with _timer_window(app, tmp_path, locale, size) as window:
            screen = window.timer_screen
            _assert_whole(screen.primary_button, window)
            _assert_label_room(screen.primary_button)
            _assert_whole(screen.clock, window)
            # Nothing else on the Timer page is cut off at the page edge.
            assert screen.minimumSizeHint().width() <= window.page_stack.width()


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_controls_keep_one_line_labels_while_they_fit(app, locale):
    # At the smallest font no label needs to wrap, so every button keeps
    # the one-line size it had before the width budget existed.
    with _scaled_font(app, 9.0):
        screen = TimerScreen(Strings(locale), SETTINGS)
        _render(screen, "idle", skip_destination="short_break")
        screen.resize(screen.minimumSizeHint())
        screen.show()
        _process(app, 12)
        try:
            for button in (screen.primary_button, screen.skip_button):
                one_line = QPushButton.sizeHint(button)
                assert button.minimumSizeHint().width() == one_line.width()
                assert button.width() >= one_line.width()
                assert button.height() == one_line.height()
        finally:
            screen.close()
            screen.deleteLater()
            _process(app)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_secondary_buttons_fit_the_column_at_the_largest_font(app, tmp_path, locale):
    with _scaled_font(app, 30.0):
        with _timer_window(app, tmp_path, locale, "minimum") as window:
            screen = window.timer_screen
            _render(screen, "running")
            _process(app, 12)
            viewport = screen.controls_scroll.viewport()
            for button in (screen.finish_button, screen.cancel_button):
                rect = _rect_in(button, viewport)
                # The row may scroll vertically, never sideways.
                assert rect.left() >= 0 and rect.right() < viewport.width(), (
                    rect,
                    viewport.rect(),
                )
                _assert_label_room(button)
            finish = _rect_in(screen.finish_button, viewport)
            assert not finish.intersects(_rect_in(screen.cancel_button, viewport))


def test_a_narrow_primary_paints_its_label_on_whole_words(app, tmp_path):
    # Only the pseudo-locale label is wider than the minimum window at 30 pt.
    painted: list[QStyleOptionButton] = []

    def record(painter, element, option):
        if element == QStyle.ControlElement.CE_PushButton:
            painted.append(QStyleOptionButton(option))

    with _scaled_font(app, 30.0):
        with _timer_window(app, tmp_path, "ar-XB", "minimum") as window:
            primary = window.timer_screen.primary_button
            assert primary.width() < QPushButton.sizeHint(primary).width()
            with patch.object(QStylePainter, "drawControl", record):
                primary.repaint()
            assert painted, "the narrow primary did not paint through the style"
            lines = painted[-1].text.split("\n")
            assert len(lines) > 1
            # Breaks fall between words only; the accessible text is unchanged.
            assert " ".join(lines).split() == primary.text().split()
            assert "\n" not in primary.text()
            metrics = primary.fontMetrics()
            chrome = QPushButton.sizeHint(primary).width() - metrics.horizontalAdvance(primary.text())
            for line in lines:
                # A single word wider than the button keeps its own line.
                if len(line.split()) > 1:
                    assert metrics.horizontalAdvance(line) <= primary.width() - chrome, line


# From 24 pt the settings column alone needs more width than the window
# minimum may take on an 800-pixel screen (D76 caps it at the screen width),
# so this case stops at the largest font that can fit beside the dial.
@pytest.mark.parametrize("points", (18.0, 20.0))
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_primary_stays_whole_with_the_settings_column_shown(app, tmp_path, locale, points):
    # The settings column takes width beside the controls, so the width
    # budget must shrink when it shows.
    with _scaled_font(app, points):
        with _timer_window(app, tmp_path, locale, "minimum") as window:
            window.settings_button.setChecked(True)
            _process(app, 12)
            window.resize(window.minimumSize())
            _process(app, 12)
            screen = window.timer_screen
            assert screen.settings_scroll.isVisible()
            _assert_whole(screen.primary_button, window)
            _assert_label_room(screen.primary_button)
            assert screen.minimumSizeHint().width() <= window.page_stack.width()


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_showing_settings_rebudgets_without_a_resize(app, locale):
    # Toggling the settings column keeps the page width, so no resize event
    # reaches the Timer screen. The host is not a window, so the screen
    # cannot grow either; its controls must fit the width it already has.
    host = QWidget()
    with _scaled_font(app, 20.0):
        screen = TimerScreen(Strings(locale), SETTINGS)
        screen.setParent(host)
        _render(screen, "idle", skip_destination="short_break")
        screen.apply_responsive_layout(landscape=True, compact=False)
        host.resize(800, 700)
        screen.setGeometry(host.rect())
        host.show()
        _process(app, 12)
        try:
            screen.set_settings_visible(True)
            _process(app, 12)
            assert screen.size() == host.size()
            assert screen.minimumSizeHint().width() <= screen.width()
            _assert_label_room(screen.primary_button)
            _assert_label_room(screen.skip_button)
        finally:
            host.close()
            host.deleteLater()
            _process(app)


@pytest.mark.parametrize("points", (9.0, 30.0))
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_width_budgets_leave_the_column_chrome_inside_the_measured_room(
    app, tmp_path, locale, points
):
    # The budgets are the measured room minus the controls column's own
    # chrome: its frame on both sides and its layout margins. A budget that
    # skips the frame lets a wrapped label ask for a few pixels more than
    # the room, which the clock's stretch slack hides in the whole-page cases.
    with _scaled_font(app, points):
        with _timer_window(app, tmp_path, locale, "minimum") as window:
            screen = window.timer_screen
            column = screen.controls_column
            # The style sheet border gives the column a frame; without one
            # this case could not tell the frame apart from no frame.
            assert column.frameWidth() > 0
            margins = column.layout().contentsMargins()
            room = screen._controls_room() - screen.controls_scroll.bar_allowance()
            inner = room - column.frameWidth() * 2 - margins.left() - margins.right()
            assert screen.primary_button._width_budget == inner
            row = [
                button
                for button in (
                    screen.finish_button,
                    screen.cancel_button,
                    screen.skip_button,
                    screen.stop_sound_button,
                )
                if not button.isHidden()
            ]
            assert row
            spacing = screen._secondary_layout.spacing() * (len(row) - 1)
            for button in row:
                # The widest equal share that still fits the inner width.
                share = button._width_budget
                assert share * len(row) + spacing <= inner
                assert (share + 1) * len(row) + spacing > inner
            assert column.minimumSizeHint().width() <= room
