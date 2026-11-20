"""UX round 3: Timer controls flow, wrapped heights and window width.

DSK-05: the completed state with the stop sound button widened the controls
column and moved the dial. DSK-22: three secondary actions shared one row in
thirds, so a long word outgrew its third and the column outgrew the page,
cutting the primary. DSK-23: the style sheet min-height became an explicit
minimum that the layout kept over the taller wrapped hint. DSK-24: at ar-XB
9 pt Skip wrapped although a line could hold it. DSK-14: the style sheet
min-width squeezed the Account label below its hint. DSK-12: closing
Settings kept the width that opening it forced.

Every expectation is measured from the widgets; no pixel constant is used.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget
from test_r2_c_m3_timer_width import (
    POINT_SIZES,
    _assert_label_room,
    _assert_whole,
    _rect_in,
    _timer_window,
)
from test_r52_d05_nav_reflow import _process, _scaled_font
from test_ux_timer_controls import _render

from pomodorough.account_resolution_controller import AccountPresentation
from pomodorough.timer_action_flow import TimerActionFlow, fill_line, pack_lines
from pomodorough.wrapping_push_button import WrappingPushButton

SOUND_STATES = {
    "idle_sound": {
        "status": "idle",
        "skip_destination": "long_break",
        "sound_active": True,
    },
    "running_sound": {"status": "running", "sound_active": True},
    "completed_sound": {
        "status": "completed",
        "skip_destination": "short_break",
        "sound_active": True,
    },
}
QUIET_STATES = {
    "idle": {"status": "idle", "skip_destination": "short_break"},
    "running": {"status": "running"},
}


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _show_state(app: QApplication, window, options: dict) -> None:
    options = dict(options)
    _render(window.timer_screen, options.pop("status"), **options)
    _process(app, 12)


def _secondary_actions(screen) -> list[QPushButton]:
    row = (
        screen.finish_button,
        screen.cancel_button,
        screen.skip_button,
        screen.stop_sound_button,
    )
    return [button for button in row if button.isVisibleTo(screen)]


def _assert_controls_whole(window, label: str) -> None:
    """Primary and dial whole; each secondary inside the column at full size."""
    screen = window.timer_screen
    _assert_whole(screen.primary_button, window)
    _assert_label_room(screen.primary_button)
    _assert_whole(screen.clock, window)
    assert screen.minimumSizeHint().width() <= window.page_stack.width(), label
    viewport = screen.controls_scroll.viewport()
    shown = _secondary_actions(screen)
    assert shown, label
    for button in shown:
        rect = _rect_in(button, viewport)
        assert rect.left() >= 0 and rect.right() < viewport.width(), (
            label,
            button.text(),
            rect,
        )
        _assert_label_room(button)
    for index, first in enumerate(shown):
        for second in shown[index + 1 :]:
            assert not _rect_in(first, viewport).intersects(
                _rect_in(second, viewport)
            ), label


def _one_line(button: QPushButton) -> QSize:
    return QPushButton.sizeHint(button)


# Flow arithmetic -----------------------------------------------------------


def test_lines_hold_items_up_to_the_exact_room():
    # 40 + 8 + 52 is exactly 100: the second item shares the line. One more
    # pixel of spacing would not fit, so it would start a new line.
    assert pack_lines([40, 52], 100, 8) == [[0, 1]]
    assert pack_lines([40, 52], 100, 9) == [[0], [1]]
    assert pack_lines([40, 53], 100, 8) == [[0], [1]]


def test_items_keep_their_order_and_an_oversized_item_takes_its_own_line():
    assert pack_lines([30, 120, 30, 30], 100, 8) == [[0], [1], [2, 3]]
    assert pack_lines([], 100, 8) == []


def test_a_line_splits_its_room_evenly_without_cutting_an_item():
    assert fill_line([40, 30], 100, 8) == [46, 46]
    # The wide item keeps its width; the others split the rest.
    assert fill_line([70, 10, 10], 116, 8) == [70, 15, 15]
    assert sum(fill_line([33, 20, 20], 101, 8)) + 16 == 101
    # An item wider than the room keeps its width.
    assert fill_line([120], 100, 8) == [120]


def _flow_host(direction: Qt.LayoutDirection) -> tuple[QWidget, list[QPushButton]]:
    host = QWidget()
    host.setLayoutDirection(direction)
    flow = TimerActionFlow(host)
    flow.setSpacing(8)
    flow.setContentsMargins(0, 0, 0, 0)
    buttons = [QPushButton(text) for text in ("first", "second", "third")]
    for button in buttons:
        flow.addWidget(button)
    return host, buttons


@pytest.mark.parametrize(
    "direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft]
)
def test_flow_wraps_at_its_width_and_mirrors_for_right_to_left(app, direction):
    host, buttons = _flow_host(direction)
    try:
        widths = [button.sizeHint().width() for button in buttons]
        flow = host.layout()
        # Room for the first two side by side, not for the third as well.
        room = widths[0] + widths[1] + flow.spacing()
        assert flow.minimumSize().width() == max(widths)
        assert flow.sizeHint().width() == sum(widths) + 2 * flow.spacing()
        height = flow.heightForWidth(room)
        host.resize(room, height)
        host.show()
        _process(app, 4)
        first, second, third = (button.geometry() for button in buttons)
        assert first.top() == second.top() < third.top()
        assert third.top() == first.bottom() + 1 + flow.spacing()
        assert height == third.bottom() + 1
        if direction == Qt.LayoutDirection.LeftToRight:
            assert first.right() < second.left()
        else:
            assert second.right() < first.left()
        # A line of one fills the whole room.
        assert third.width() == room
    finally:
        host.close()


# DSK-22, DSK-23, DSK-24: every sound state at every font --------------------


@pytest.mark.parametrize("size", ["default", "minimum"])
@pytest.mark.parametrize("points", POINT_SIZES)
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("state", ["idle_sound", "running_sound"])
def test_timer_controls_stay_whole_while_the_sound_plays(
    app, tmp_path, state, locale, points, size
):
    with (
        _scaled_font(app, points),
        _timer_window(app, tmp_path, locale, size) as window,
    ):
        _show_state(app, window, SOUND_STATES[state])
        _assert_controls_whole(window, f"{state} {locale} {points} {size}")


@pytest.mark.parametrize("points", (9.0, 30.0))
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("state", ["idle", "running", "running_sound"])
def test_portrait_controls_stay_whole(app, tmp_path, state, locale, points):
    states = {**QUIET_STATES, **SOUND_STATES}
    with (
        _scaled_font(app, points),
        _timer_window(app, tmp_path, locale, "minimum") as window,
    ):
        window.resize(QSize(window.minimumWidth(), window.minimumWidth() * 3 // 2))
        _process(app, 20)
        assert not window.timer_screen._landscape
        _show_state(app, window, states[state])
        _assert_controls_whole(window, f"portrait {state} {locale} {points}")


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_labels_keep_one_line_at_the_default_window(app, tmp_path, locale):
    # DSK-24: a label wraps only when no line of the column can hold it.
    states = {**QUIET_STATES, **SOUND_STATES}
    with (
        _scaled_font(app, 9.0),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        for state, options in states.items():
            _show_state(app, window, options)
            for button in [screen.primary_button, *_secondary_actions(screen)]:
                assert button.sizeHint() == _one_line(button), (state, button.text())
                assert button.width() >= _one_line(button).width(), (
                    state,
                    button.text(),
                )
                assert button.height() == _one_line(button).height(), (
                    state,
                    button.text(),
                )


# DSK-05: the completed state leaves the dial where it was ------------------


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_completed_state_with_sound_keeps_the_dial_in_place(app, tmp_path, locale):
    with (
        _scaled_font(app, 9.0),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, QUIET_STATES["idle"])
        clock = _rect_in(screen.clock, window)
        panel = _rect_in(screen.controls_panel, window)
        _show_state(app, window, SOUND_STATES["completed_sound"])
        assert _rect_in(screen.clock, window) == clock
        assert _rect_in(screen.controls_panel, window) == panel
        _assert_controls_whole(window, f"completed {locale}")
        # The whole flow shows without scrolling.
        assert not screen.controls_scroll.verticalScrollBar().isVisible()


@pytest.mark.parametrize("state", ["idle_sound", "running_sound", "completed_sound"])
def test_controls_height_follows_the_flow_when_the_window_widens(app, tmp_path, state):
    # At the minimum window the flow needs two lines. A wider window holds
    # it on one line, and the controls must give the second line's height
    # back instead of keeping the height measured at the old width.
    with (
        _scaled_font(app, 9.0),
        _timer_window(app, tmp_path, "en", "minimum") as window,
    ):
        screen = window.timer_screen
        column = screen.controls_column
        _show_state(app, window, SOUND_STATES[state])
        narrow = column.heightForWidth(column.width())
        assert len({button.y() for button in _secondary_actions(screen)}) > 1
        window.resize(window.minimumWidth() * 3 // 2, window.minimumHeight())
        _process(app, 12)
        assert len({button.y() for button in _secondary_actions(screen)}) == 1
        flowed = column.heightForWidth(column.width())
        assert flowed < narrow
        assert screen.controls_scroll.height() == flowed
        assert not screen.controls_scroll.verticalScrollBar().isVisible()
        _assert_controls_whole(window, f"widened {state}")


# DSK-23: explicit style sheet minimums do not cut wrapped labels -----------


def test_wrapped_label_keeps_its_height_when_the_parent_is_at_its_minimum(app):
    host = QWidget()
    host.setStyleSheet("QPushButton { min-height: 29px; padding: 3px 7px; }")
    layout = QVBoxLayout(host)
    button = WrappingPushButton("several words that cannot share one short line")
    layout.addWidget(button)
    try:
        host.show()
        _process(app, 4)
        one_line = _one_line(button)
        button.set_width_budget(one_line.width() // 3)
        _process(app, 4)
        assert button.sizeHint().height() > one_line.height()
        assert button.minimumSize().isNull()
        host.resize(host.minimumSizeHint())
        _process(app, 4)
        assert button.height() >= button.sizeHint().height()
        # A later style sheet change polishes again; the minimum stays clear.
        host.setStyleSheet("QPushButton { min-height: 31px; padding: 3px 7px; }")
        _process(app, 4)
        assert button.minimumSize().isNull()
        assert button.sizeHint().height() >= _one_line(button).height()
    finally:
        host.close()


# DSK-14: the Account label keeps its hint at 1.5x ar-XB ---------------------


def test_account_label_keeps_its_width_with_settings_open(app, tmp_path):
    base = QApplication.font().pointSizeF()
    with (
        _scaled_font(app, base * 1.5),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        window.settings_button.setChecked(True)
        _process(app, 12)
        window.resize(window.minimumSize())
        _process(app, 12)
        account = window.account_button
        assert account.width() >= account.sizeHint().width()
        # The authenticated style polishes directly through the style.
        for authenticated in (True, False):
            window._present_account(
                AccountPresentation(
                    authenticated=authenticated,
                    text=window.strings.text("account.sign_in"),
                    tooltip="",
                    accessible_name="",
                )
            )
            _process(app, 12)
            assert account.minimumSize().isNull(), authenticated
            assert account.width() >= account.sizeHint().width(), authenticated


# DSK-12: closing Settings gives the width back ------------------------------


def test_closing_settings_restores_the_width_opening_took(app, tmp_path):
    with _timer_window(app, tmp_path, "en", "default") as window:
        before = window.size()
        window.settings_button.setChecked(True)
        _process(app, 12)
        assert window.width() > before.width()
        window.settings_button.setChecked(False)
        _process(app, 12)
        assert window.size() == before


def test_closing_settings_keeps_a_width_the_user_chose(app, tmp_path):
    with _timer_window(app, tmp_path, "en", "default") as window:
        window.settings_button.setChecked(True)
        _process(app, 12)
        chosen = window.width() + window.minimumWidth() // 4
        window.resize(chosen, window.height())
        _process(app, 12)
        window.settings_button.setChecked(False)
        _process(app, 12)
        assert window.width() == chosen


def test_closing_settings_keeps_a_window_that_was_wide_enough(app, tmp_path):
    with _timer_window(app, tmp_path, "en", "default") as window:
        window.settings_button.setChecked(True)
        _process(app, 12)
        wide = window.width()
        window.settings_button.setChecked(False)
        _process(app, 12)
        window.resize(wide, window.height())
        _process(app, 12)
        window.settings_button.setChecked(True)
        _process(app, 12)
        assert window.width() == wide
        window.settings_button.setChecked(False)
        _process(app, 12)
        assert window.width() == wide
