"""DSK-12 follow-up: the switch between the round dial and the compact readout.

Acceptance of the first follow-up left four gaps: the task title was cut
under the compact readout, the default window kept a 2 px scroll range,
the minimum window at three times the font lost more of Stop Sound than
before, and parts of the switch had no test that failed without them.

Every expectation is measured from the widgets; no pixel constant is used.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMargins, QPoint, QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QBoxLayout, QWidget
from test_dsk12_compact_readout import (
    _assert_stop_sound_on_the_page,
    _readout_side_is_whole,
    _require_compact_readout,
    _title_cut,
)
from test_r2_c_m3_timer_width import _timer_window
from test_r52_d05_nav_reflow import _process, _scaled_font
from test_ux_r3_timer_flow import QUIET_STATES, SOUND_STATES, _show_state

from pomodorough import task_picker_row

BASE_POINTS = 9.0


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _stacked(screen) -> bool:
    direction = screen.task_selector_panel.layout().direction()
    return direction == QBoxLayout.Direction.TopToBottom


def _in_viewport(widget, scroll) -> QRect:
    viewport = scroll.viewport()
    return QRect(widget.mapTo(viewport, QPoint(0, 0)), widget.size())


@pytest.mark.parametrize("label", list(SOUND_STATES))
def test_default_window_shows_sound_controls_without_a_scroll_range(
    app, tmp_path, label
):
    # Low A: at twice the font the pseudo-locale's tallest column was 2 px
    # taller than the page, so the controls kept a scroll range.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES[label])
        assert screen.controls_scroll.verticalScrollBar().maximum() == 0
        _assert_stop_sound_on_the_page(window)


def test_column_margins_give_way_by_at_most_half(app, tmp_path):
    with _timer_window(app, tmp_path, "en", "default") as window:
        screen = window.timer_screen
        layout = screen.controls_column.layout()
        full = screen.full_column_margins()
        width = screen.controls_scroll.width()
        most = min(full.top(), full.bottom()) // 2

        def trimmed(by: int) -> QMargins:
            return QMargins(
                full.left(), full.top() - by, full.right(), full.bottom() - by
            )

        screen.follow_dial_form(width, 3)
        assert layout.contentsMargins() == trimmed(2)
        screen.follow_dial_form(width, 2 * most)
        assert layout.contentsMargins() == trimmed(most)
        screen.follow_dial_form(width, 2 * most + 1)
        assert layout.contentsMargins() == full
        screen.follow_dial_form(width, 3)
        screen.follow_dial_form(None)
        assert layout.contentsMargins() == full


@pytest.mark.parametrize("label", ["idle_sound", "completed_sound"])
def test_narrow_readout_side_stacks_the_picker_label(app, tmp_path, label):
    # Low B: under the compact readout the picker had the side's width
    # minus its label, and the task title was cut.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES[label])
        # Whether the label goes above the picker depends on the font: a
        # font with narrower letters shows the title whole beside the
        # label. Either way the title is whole and the readout keeps its
        # least height.
        assert _readout_side_is_whole(screen)


def test_readout_side_keeps_the_picker_row_width_while_the_controls_fit(app, tmp_path):
    # Low B: the side counts the label beside the picker, which leaves the
    # buttons less width. That wins whenever the buttons still fit the
    # page; a side only as wide as the stacked picker cut the title here.
    with (
        _scaled_font(app, BASE_POINTS * 1.75),
        _timer_window(app, tmp_path, "ar-XB", "minimum") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES["idle_sound"])
        assert _title_cut(screen.task_combo) <= 0
        _assert_stop_sound_on_the_page(window)


def test_round_dial_puts_the_picker_label_back_beside_it(app, tmp_path):
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES["idle_sound"])
        if not _stacked(screen):
            # A font with narrower letters keeps the label beside the
            # picker here; stack it so the switch has something to undo.
            task_picker_row.set_stacked(screen.task_selector_panel, True)
        window.resize(window.width() * 3, window.height() * 3)
        _process(app, 12)
        assert not screen.clock.compact_readout
        assert not _stacked(screen)


def test_minimum_window_at_three_times_the_font_keeps_the_round_dial(app, tmp_path):
    # Low C: both forms scroll here. The compact readout's column was
    # taller than the round dial's below the task picker, so the swap
    # moved Stop Sound further below the fold.
    with (
        _scaled_font(app, BASE_POINTS * 3),
        _timer_window(app, tmp_path, "ar-XB", "minimum") as window,
    ):
        _show_state(app, window, SOUND_STATES["running_sound"])
        assert not window.timer_screen.clock.compact_readout


def test_scroll_brings_stop_sound_as_far_as_the_primary_allows(app, tmp_path):
    # Low C: the scroll kept only the primary action whole. While a sound
    # plays it also brings Stop Sound up, until the primary action reaches
    # the top of the viewport.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "minimum") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        scroll = screen.controls_scroll
        assert not screen.clock.compact_readout
        assert scroll.verticalScrollBar().maximum() > 0
        viewport = scroll.viewport().rect()
        primary = _in_viewport(screen.primary_button, scroll)
        stop = _in_viewport(screen.stop_sound_button, scroll)
        assert viewport.contains(primary)
        assert viewport.contains(stop) or primary.top() == 0, (primary, stop)


@pytest.mark.parametrize(
    ("sound", "quiet"), [("running_sound", "running"), ("idle_sound", "idle")]
)
def test_scroll_goes_back_to_the_picker_when_stop_sound_hides(
    app, tmp_path, sound, quiet
):
    # Low G: the scroll toward Stop Sound stayed after the sound stopped,
    # and the task picker was left mostly above the viewport.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "minimum") as window,
    ):
        screen = window.timer_screen
        scroll = screen.controls_scroll
        _show_state(app, window, SOUND_STATES[sound])
        assert not screen.clock.compact_readout
        assert scroll.verticalScrollBar().value() > 0
        _show_state(app, window, QUIET_STATES[quiet])
        viewport = scroll.viewport().rect()
        picker = _in_viewport(screen.task_selector_panel, scroll)
        primary = _in_viewport(screen.primary_button, scroll)
        assert viewport.contains(primary)
        assert viewport.contains(picker) or primary.bottom() == viewport.bottom(), (
            picker,
            primary,
        )


def test_scroll_back_to_the_picker_ends_with_the_relayout(app, tmp_path):
    # Low G: once the column has its new size, a later relayout scrolls
    # only as far as the primary action needs again, so a user scroll
    # toward the secondary row stays.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "ar-XB", "minimum") as window,
    ):
        screen = window.timer_screen
        bar = screen.controls_scroll.verticalScrollBar()
        _show_state(app, window, SOUND_STATES["idle_sound"])
        _show_state(app, window, QUIET_STATES["idle"])
        bar.setValue(bar.maximum())
        user_scroll = bar.value()
        screen.controls_scroll.keep_primary_whole()
        assert bar.value() == user_scroll


def test_default_window_at_two_and_a_half_times_the_font_keeps_the_title_whole(
    app, tmp_path
):
    # Note H: both picker forms scroll here. The row width still keeps the
    # compact readout ahead of the round dial and costs Stop Sound only a
    # few pixels, so the title gets the width.
    with (
        _scaled_font(app, BASE_POINTS * 2.5),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES["completed_sound"])
        assert screen.controls_scroll.verticalScrollBar().maximum() > 0
        panel = screen.task_selector_panel
        assert panel.width() >= task_picker_row.row_width(panel, preferred=False)
        # With the label above it the picker has the side's whole width;
        # a title wider than that at the host font has no more to get.
        assert _title_cut(screen.task_combo) <= 0 or _stacked(screen)


def test_default_window_at_three_times_the_font_stacks_the_label_for_stop_sound(
    app, tmp_path
):
    # Note H: here the row width would bring the round dial back and move
    # Stop Sound about twice as far below the fold, so the label stacks
    # and the title is cut instead.
    with (
        _scaled_font(app, BASE_POINTS * 3),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        _show_state(app, window, SOUND_STATES["completed_sound"])
        assert _stacked(screen)


@pytest.mark.parametrize(
    ("fitting", "row_beats_round_dial", "expected"),
    [
        ({"row", "stacked"}, False, "row"),
        ({"stacked"}, True, "stacked"),
        (set(), True, "row"),
        (set(), False, "stacked"),
    ],
)
def test_readout_side_width_choice(
    app, tmp_path, monkeypatch, fitting, row_beats_round_dial, expected
):
    # Lows B and C, note H: the title keeps the row width while the
    # controls fit with it. Stop Sound wins when only the stacked label
    # fits them. When both scroll, the row width wins only while the
    # compact readout still beats the round dial with it.
    with (
        _scaled_font(app, BASE_POINTS * 2.5),
        _timer_window(app, tmp_path, "ar-XB", "default") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["completed_sound"])
        panel = screen.task_selector_panel
        readout = screen.clock.readout_minimum().width()
        widths = {
            "row": max(readout, task_picker_row.row_width(panel, preferred=False)),
            "stacked": max(readout, task_picker_row.stacked_width(panel)),
        }
        assert widths["stacked"] < widths["row"]
        page = screen.left_layout.geometry().size()
        room = screen.room_below_readout(page.height())
        assert task_picker_row.suits_stacking(panel, widths["stacked"], room)
        fitting_widths = {widths[name] for name in fitting}
        form = screen.dial_form

        def fit(page, side):
            return 0, page.height() + (0 if side in fitting_widths else 1)

        monkeypatch.setattr(form, "_compact_fit", fit)
        monkeypatch.setattr(
            form, "_compact_beats_round_dial", lambda page, side: row_beats_round_dial
        )
        assert form.compact_side_width() == widths[expected]


@pytest.mark.parametrize(
    ("shorter", "longer"), [("9:59", "10:00"), ("59:59", "1:00:00")]
)
def test_new_countdown_digit_reaches_the_layout(app, tmp_path, shorter, longer):
    # Low D: the compact readout is as wide as its countdown, so a digit
    # more or less must reach the layout without a resize.
    with (
        _scaled_font(app, BASE_POINTS * 1.5),
        _timer_window(app, tmp_path, "en", "default") as window,
    ):
        clock = window.timer_screen.clock
        _show_state(app, window, QUIET_STATES["idle"])
        assert clock.compact_readout
        readout = window.timer_screen.readout_layout
        item = readout.itemAt(readout.indexOf(clock))
        for text in (shorter, longer, shorter):
            clock.set_state(text, "Focus", "Running", 0.5)
            assert item.sizeHint() == clock.sizeHint(), text


def _grow_font(widget: QWidget, factor: float) -> None:
    font = QFont(widget.font())
    font.setPointSizeF(widget.font().pointSizeF() * factor)
    widget.setFont(font)


def test_font_change_alone_refits_the_dial(app, tmp_path):
    # Low D: a larger font with no resize must still bring the compact
    # readout. The children set their own fonts, so growing them first
    # sends the screen no event; only its own font change starts the fit.
    with _timer_window(app, tmp_path, "en", "default") as window:
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        size = screen.size()
        for child in screen.findChildren(QWidget):
            _grow_font(child, 2.5)
        _process(app, 12)
        assert screen.size() == size
        assert not screen.clock.compact_readout
        _grow_font(screen, 2.5)
        _process(app, 12)
        assert screen.size() == size
        assert screen.clock.compact_readout
