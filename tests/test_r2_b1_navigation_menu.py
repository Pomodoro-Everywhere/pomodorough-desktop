"""UX round 2 B1: the navigation collapses into a menu; Timer keeps its primary.

A navigation row that does not fit the header collapses into one menu
button. It never stacks, so the header keeps its one-row height and the
window minimum never grows for it. The Timer controls column scrolls
inside its wrapper so the primary action keeps its height.

The collapse is forced from measured widths: a pseudo-long destination
label makes the measured row wider than the header has room for. No
assertion depends on the platform font or on pixel constants.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QWidget
from test_r52_d05_nav_reflow import _main_window, _process, _scaled_font
from test_ux_timer_controls import SETTINGS, _render

from pomodorough.localization import Strings
from pomodorough.screen_navigation import ScreenNavigation
from pomodorough.task_undo_banner import TaskUndoBanner
from pomodorough.timer_screen import TimerScreen

LONG_LABEL_REPEAT = 12


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _long_tasks_label():
    """Build windows whose Tasks label is far wider than any header."""
    original = Strings.text

    def text(self, key, **values):
        value = original(self, key, **values)
        if key == "nav.tasks":
            return " ".join([value] * LONG_LABEL_REPEAT)
        return value

    with patch.object(Strings, "text", text):
        yield


def _rect_in(widget: QWidget, ancestor: QWidget) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _show_tasks(app: QApplication, window) -> None:
    window.navigation.screen_requested.emit(1)
    _process(app)
    window.tasks_page.verticalScrollBar().setValue(0)
    _process(app)


def _shrink_to_minimum(app: QApplication, window) -> None:
    window.resize(window.minimumSize())
    _process(app, 12)
    assert window.size() == window.minimumSize()


def _assert_add_button_in_view(window) -> None:
    viewport = window.tasks_page.viewport()
    add = _rect_in(window.tasks_screen.add_task_button, viewport)
    assert window.tasks_page.verticalScrollBar().value() == 0
    assert viewport.rect().contains(add), (add, viewport.rect())


def _assert_menu_button_whole(window) -> None:
    menu_button = window.navigation.menu_button
    assert menu_button.isVisible()
    assert menu_button.visibleRegion().boundingRect() == menu_button.rect()
    centre = menu_button.mapTo(window, menu_button.rect().center())
    assert window.childAt(centre) is menu_button


@pytest.mark.parametrize("window_size", ["default", "minimum"])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_long_label_collapses_without_growing_header(
    app, tmp_path, locale, window_size
):
    # The smallest supported font in a screen-wide window lets the real
    # labels fit one row in both locales, so the reference is one row.
    with _scaled_font(app, 9.0):
        _assert_long_label_collapse(app, tmp_path, locale, window_size)


def _assert_long_label_collapse(app, tmp_path, locale, window_size) -> None:
    wide = QApplication.primaryScreen().availableGeometry().width()
    with _main_window(app, tmp_path / "row", locale, wide, 360) as reference:
        assert reference.navigation._nav_mode == "row"
        row_header = reference._header_band.height()
        row_minimum = reference.minimumSize()
    with _long_tasks_label():
        with _main_window(app, tmp_path / "menu", locale, wide, 360) as window:
            if window_size == "minimum":
                _shrink_to_minimum(app, window)
            navigation = window.navigation
            need_row, _need_menu = navigation.measured_nav_needs()
            assert need_row > window._header_nav_avail()
            assert navigation._nav_mode == "menu"
            assert all(button.isHidden() for button in navigation.buttons)
            assert window._header_band.height() == row_header
            assert window.minimumSize() == row_minimum
            _assert_menu_button_whole(window)
            _show_tasks(app, window)
            _assert_add_button_in_view(window)


def test_menu_lists_and_activates_every_destination(app, tmp_path):
    with _long_tasks_label():
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            navigation = window.navigation
            assert navigation._nav_mode == "menu"
            texts = [action.text() for action in navigation.destination_actions]
            assert texts == [button.text() for button in navigation.buttons]
            for index, action in enumerate(navigation.destination_actions):
                action.trigger()
                _process(app)
                assert window.page_stack.currentIndex() == index
                assert action.isChecked()
                assert navigation.buttons[index].isChecked()
                assert sum(a.isChecked() for a in navigation.destination_actions) == 1
                destination = window.strings.text(
                    f"destination.{('timer', 'tasks', 'arrivals', 'network')[index]}"
                )
                expected = window.strings.text(
                    "nav.menu_accessible", destination=destination
                )
                assert navigation.menu_button.accessibleName() == expected


def test_menu_label_elides_and_keeps_full_name_in_tooltip(app, tmp_path):
    with _long_tasks_label():
        with _main_window(app, tmp_path, "en", 640, 360) as window:
            navigation = window.navigation
            navigation.destination_actions[1].trigger()
            _process(app)
            full = navigation.buttons[1].text()
            shown = navigation.menu_button.text()
            assert shown != full
            assert shown.endswith("…")
            assert navigation.menu_button.toolTip() == full
            assert navigation.menu_button.width() <= window._header_nav_avail()
            navigation.destination_actions[0].trigger()
            _process(app)
            # A label that fits is shown whole and needs no tooltip.
            assert navigation.menu_button.text() == navigation.buttons[0].text()
            assert navigation.menu_button.toolTip() == ""


def test_collapse_hands_focus_to_menu_and_back(app):
    host = QWidget()
    navigation = ScreenNavigation(Strings("en"), host)
    host.show()
    host.activateWindow()
    _process(app)
    try:
        navigation.select(2)
        navigation.buttons[2].setFocus(Qt.FocusReason.ShortcutFocusReason)
        _process(app)
        assert QApplication.focusWidget() is navigation.buttons[2]
        navigation.set_nav_mode("menu")
        _process(app)
        assert QApplication.focusWidget() is navigation.menu_button
        navigation.set_nav_mode("row")
        _process(app)
        assert QApplication.focusWidget() is navigation.buttons[2]
    finally:
        host.close()
        host.deleteLater()
        _process(app)


def test_empty_tasks_hint_wraps_instead_of_widening_page(app, tmp_path):
    with _scaled_font(app, 30.0):
        with _main_window(app, tmp_path, "ar-XB", 640, 360) as window:
            _show_tasks(app, window)
            hint = window.tasks_screen.tasks_empty
            assert hint.wordWrap()
            advance = hint.fontMetrics().horizontalAdvance(hint.text())
            assert window.tasks_screen.minimumSizeHint().width() < advance
            assert window.tasks_screen.width() <= window.tasks_page.viewport().width()


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_timer_primary_keeps_its_height_in_a_short_screen(app, locale):
    with _scaled_font(app, 30.0):
        screen = TimerScreen(Strings(locale), SETTINGS)
        _render(screen, "idle", skip_destination="short_break")
        screen.resize(screen.minimumSizeHint())
        screen.show()
        _process(app, 12)
        try:
            _assert_primary_whole(screen)
            scroll = screen.controls_scroll
            column = screen.controls_column
            assert column.height() >= column.minimumSizeHint().height()
            assert column.height() > scroll.viewport().height()
            skip = _rect_in(screen.skip_button, column)
            assert not skip.intersects(_rect_in(screen.primary_button, column))
            # Wheel or focus may scroll the secondary row into view; the
            # next resize scrolls the primary action back into full view.
            scroll.ensureWidgetVisible(screen.skip_button, 0, 0)
            _process(app)
            screen.resize(screen.width(), screen.height() + 1)
            _process(app, 12)
            _assert_primary_whole(screen)
        finally:
            screen.close()
            screen.deleteLater()
            _process(app)


def _assert_primary_whole(screen: TimerScreen) -> None:
    viewport = screen.controls_scroll.viewport()
    primary = _rect_in(screen.primary_button, viewport)
    assert viewport.rect().contains(primary), (primary, viewport.rect())
    assert primary.height() >= screen.primary_button.minimumSizeHint().height()
    assert screen.rect().contains(_rect_in(screen.clock, screen))


@pytest.mark.parametrize(
    ("locale", "label", "accessible"),
    [
        ("en", "DELETE NOW", "Delete Plan trip now"),
        ("ar-XB", "⟦‼ DËLËTË NØW ‼⟧", "⟦‼ Dëlëtë Plan trip nøw ‼⟧"),
    ],
)
def test_undo_banner_second_button_says_delete_now(app, locale, label, accessible):
    # The button completes the deletion at once, so it is labelled for that.
    banner = TaskUndoBanner(Strings(locale))
    try:
        banner.present("Plan trip")
        assert banner.dismiss_button.text() == label
        assert banner.dismiss_button.accessibleName() == accessible
    finally:
        banner.deleteLater()
        _process(app)
