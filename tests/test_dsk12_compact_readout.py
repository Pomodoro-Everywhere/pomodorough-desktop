"""UX round 3 DSK-12: the compact readout keeps Stop Sound on the page.

The round dial keeps its size at every font, so from about 1.5x font the
controls beside it were taller than the page: they scrolled, and Stop Sound
fell below the fold. Like the Apple clients at accessibility text sizes,
the Timer screen now shows a compact readout when the controls beside the
round dial do not fit the page.

Every expectation is measured from the widgets; no pixel constant is used.
The host font decides how much the readout's side holds at one scale, so a
case that needs the compact readout measures first whether its page can
hold it (R55-D01).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionComboBox, QWidget
from test_r2_c_m3_timer_width import _assert_whole, _rect_in, _timer_window
from test_r52_d05_nav_reflow import _process, _scaled_font
from test_ux_r3_timer_flow import QUIET_STATES, SOUND_STATES, _show_state

from pomodorough import task_picker_row
from pomodorough.railway_palette import TRACK
from pomodorough.timer_action_labels import primary_labels
from pomodorough.timer_dial_form import TimerDialForm
from pomodorough.timer_readout_face import LEAST_SCALE, ReadoutFace, ReadoutState
from pomodorough.timer_view import ClockWidget

BASE_POINTS = 9.0
LOCALES = ("en", "ar-XB")
ALL_STATES = {**QUIET_STATES, **SOUND_STATES}
# Font steps, largest first, tried by a case that needs the compact readout
# and no exact font size.
COMPACT_READOUT_SCALES = (2.0, 1.75, 1.5)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _assert_stop_sound_on_the_page(window) -> None:
    """Stop Sound is whole inside the controls without any scrolling."""
    screen = window.timer_screen
    viewport = screen.controls_scroll.viewport()
    assert screen.controls_scroll.verticalScrollBar().value() == 0
    stop = _rect_in(screen.stop_sound_button, viewport)
    assert viewport.rect().contains(stop), (stop, viewport.rect())
    _assert_whole(screen.stop_sound_button, window)


def _title_cut(combo) -> int:
    """How much wider the shown task title is than the combo's text field."""
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    field = combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox,
        option,
        QStyle.SubControl.SC_ComboBoxEditField,
        combo,
    )
    return combo.fontMetrics().horizontalAdvance(combo.currentText()) - field.width()


def _readout_side_is_whole(screen) -> bool:
    """The compact readout keeps its least height and the title is whole."""
    clock = screen.clock
    return (
        clock.height() >= clock.readout_minimum().height()
        and _title_cut(screen.task_combo) <= 0
    )


def _page_would_hold_the_compact_readout(app, window) -> bool:
    """Lay the page out with the compact readout whatever the fit says.

    The form settles once, as it does for the window and the font, and the
    sound states then come without a new choice. The page holds the
    compact readout when, in every sound state, the readout keeps its
    least height, the picker shows its title whole and the controls do not
    scroll. The round dial comes back afterwards.
    """
    screen = window.timer_screen
    bar = screen.controls_scroll.verticalScrollBar()
    held = True
    with patch.object(TimerDialForm, "compact_readout_suits", return_value=True):
        screen.dial_form.settle()
        _process(app, 12)
        for state in SOUND_STATES.values():
            _show_state(app, window, state)
            held = held and _readout_side_is_whole(screen) and bar.maximum() == 0
    screen.dial_form.settle()
    _process(app, 12)
    return held


def _require_compact_readout(app, window) -> None:
    """Skip a case whose page cannot hold the compact readout at the host font.

    A font with a taller line or wider letters leaves the readout's side
    less room at the same scale, and the round dial stays. The round dial
    is a failure when the page would hold the compact readout.
    """
    if window.timer_screen.clock.compact_readout:
        return
    assert not _page_would_hold_the_compact_readout(app, window)
    pytest.skip("the host font leaves this page no room for the compact readout")


@contextmanager
def _font_showing_the_compact_readout(app, window) -> Iterator[None]:
    """Grow the font in the window to the largest step that shows the
    compact readout. The step depends on the host font."""
    for scale in COMPACT_READOUT_SCALES:
        with _scaled_font(app, BASE_POINTS * scale):
            _process(app, 12)
            if window.timer_screen.clock.compact_readout:
                yield
                return
    with _scaled_font(app, BASE_POINTS * COMPACT_READOUT_SCALES[0]):
        _process(app, 12)
        _require_compact_readout(app, window)
        yield


def _tab_stops(window) -> list[QWidget]:
    """Visible keyboard stops of the window in Tab order."""
    stops = []
    widget = window.nextInFocusChain()
    while widget is not window:
        if widget.isVisible() and widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
            stops.append(widget)
        widget = widget.nextInFocusChain()
    return stops


def _timer_stops(window) -> list[str]:
    """Dial, task picker, primary and the shown actions, in Tab order."""
    screen = window.timer_screen
    named = {
        "dial": [screen.clock],
        "picker": screen.task_selector_panel.findChildren(QWidget),
        "primary": [screen.primary_button],
        "finish": [screen.finish_button],
        "cancel": [screen.cancel_button],
        "stop": [screen.stop_sound_button],
    }
    order = []
    for stop in _tab_stops(window):
        for name, widgets in named.items():
            if stop in widgets and name not in order:
                order.append(name)
    return order


@pytest.mark.parametrize("state", sorted(SOUND_STATES))
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_stop_sound_stays_on_the_page_at_the_default_window(
    app, tmp_path, scale, locale, state
):
    with (
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        _show_state(app, window, SOUND_STATES[state])
        if window.timer_screen.controls_scroll.verticalScrollBar().maximum() > 0:
            # The controls scroll: only a page that cannot hold the compact
            # readout may keep the round dial.
            _require_compact_readout(app, window)
        _assert_stop_sound_on_the_page(window)


@pytest.mark.parametrize("state", sorted(SOUND_STATES))
@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_stop_sound_stays_on_the_page_at_the_minimum_window(
    app, tmp_path, scale, locale, state
):
    with (
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, "minimum") as window,
    ):
        _show_state(app, window, SOUND_STATES[state])
        if window.timer_screen.controls_scroll.verticalScrollBar().maximum() > 0:
            # The controls scroll: only a page that cannot hold the compact
            # readout may keep the round dial.
            _require_compact_readout(app, window)
        _assert_stop_sound_on_the_page(window)


@pytest.mark.parametrize("locale", LOCALES)
def test_base_font_shows_the_round_dial_without_scrolling(app, tmp_path, locale):
    with (
        _scaled_font(app, BASE_POINTS),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        for label, state in ALL_STATES.items():
            _show_state(app, window, state)
            assert not screen.clock.compact_readout, label
            assert screen.controls_scroll.verticalScrollBar().maximum() == 0, label
            # The task picker leads the controls column beside the dial.
            assert screen.controls_column.layout().itemAt(0).widget() is (
                screen.task_selector_panel
            ), label


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", [1.5, 2.0])
def test_large_font_shows_the_compact_readout_in_every_state(
    app, tmp_path, scale, locale
):
    # The form follows the window, the font and the language only. It must
    # not change when a timer starts or a sound plays.
    with (
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        _require_compact_readout(app, window)
        for label, state in ALL_STATES.items():
            _show_state(app, window, state)
            assert screen.clock.compact_readout, label
            _assert_whole(screen.clock, window)
            _assert_whole(screen.task_selector_panel, window)
            _assert_whole(screen.primary_button, window)


@pytest.mark.parametrize("locale", LOCALES)
def test_round_dial_returns_when_the_font_goes_back(app, tmp_path, locale):
    with (
        _scaled_font(app, BASE_POINTS),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        round_size = screen.clock.size()
        with _font_showing_the_compact_readout(app, window):
            assert screen.readout_layout.indexOf(screen.task_selector_panel) >= 0
        _process(app, 12)
        assert not screen.clock.compact_readout
        assert screen.clock.size() == round_size
        column = screen.controls_column.layout()
        assert column.itemAt(0).widget() is screen.task_selector_panel
        assert screen.readout_layout.indexOf(screen.task_selector_panel) < 0
        # The width the compact form fixed is released again.
        assert screen.controls_panel.minimumWidth() < screen.controls_panel.maximumWidth()
        _assert_stop_sound_on_the_page(window)
        _assert_whole(screen.clock, window)


@pytest.mark.parametrize("locale", LOCALES)
def test_compact_readout_keeps_the_dial_name_and_description(app, tmp_path, locale):
    with (
        _scaled_font(app, BASE_POINTS),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        clock = window.timer_screen.clock
        _show_state(app, window, SOUND_STATES["running_sound"])
        name, description = clock.accessibleName(), clock.accessibleDescription()
        assert name and description
        with _font_showing_the_compact_readout(app, window):
            assert clock.accessibleName() == name
            assert clock.accessibleDescription() == description
            assert clock.focusPolicy() == Qt.FocusPolicy.StrongFocus


@pytest.mark.parametrize("locale", LOCALES)
def test_compact_readout_keeps_the_tab_order(app, tmp_path, locale):
    expected = ["dial", "picker", "primary", "finish", "cancel", "stop"]
    with (
        _scaled_font(app, BASE_POINTS),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        _show_state(app, window, SOUND_STATES["running_sound"])
        assert _timer_stops(window) == expected
        with _font_showing_the_compact_readout(app, window):
            assert _timer_stops(window) == expected
        _process(app, 12)
        assert _timer_stops(window) == expected


@pytest.mark.parametrize(
    ("size", "scale", "locale"),
    [
        *(("default", scale, locale) for scale in (1.5, 2.0) for locale in LOCALES),
        *(("minimum", 1.5, locale) for locale in LOCALES),
        ("minimum", 2.0, "en"),
        # At twice the font the pseudo-locale's controls fit beside the
        # readout only with the picker's label stacked, which would take the
        # readout's least height; the round dial stays (Low C).
        ("minimum", 1.75, "ar-XB"),
    ],
)
def test_readout_side_holds_the_readout_and_the_picker(
    app, tmp_path, size, scale, locale
):
    # Right after the switch the picker is not laid out yet. The readout's
    # side must still count it, or the picker lands on the buttons.
    with (
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, size) as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        _require_compact_readout(app, window)
        side = screen.dial_side_width_for(True)
        assert side >= screen.task_selector_panel.minimumSizeHint().width()
        assert side >= screen.clock.readout_minimum().width()
        controls = _rect_in(screen.controls_panel, window)
        for widget in (screen.clock, screen.task_selector_panel):
            assert widget.width() >= widget.minimumSizeHint().width()
            assert not _rect_in(widget, window).intersects(controls)
            _assert_whole(widget, window)
        assert _readout_side_is_whole(screen)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", [2.0, 3.0])
def test_tall_window_keeps_the_round_dial(app, tmp_path, scale, locale):
    # The readout's height follows the font: above the controls it would
    # take about as much of a tall window as the round dial's minimum. The
    # window is as tall as it is wide, the shortest that counts as tall; at
    # three times the font the controls would not fit beside the dial.
    with (
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, "minimum") as window,
    ):
        screen = window.timer_screen
        window.resize(QSize(window.minimumWidth(), window.minimumWidth()))
        _process(app, 12)
        _show_state(app, window, SOUND_STATES["running_sound"])
        assert not screen.landscape
        assert not screen.clock.compact_readout
        assert screen.controls_column.layout().itemAt(0).widget() is (
            screen.task_selector_panel
        )


def test_turning_tall_brings_the_round_dial_back_before_the_page_lays_out(
    app, tmp_path
):
    # The turn invalidates the page layout, which forgets its geometry until
    # Qt lays the page out. On Windows the form settled first, found no page
    # and kept the compact readout in the tall window.
    with (
        _scaled_font(app, BASE_POINTS * 2),
        _timer_window(app, tmp_path, "en", "minimum") as window,
    ):
        screen = window.timer_screen
        with patch.object(TimerDialForm, "compact_readout_suits", return_value=True):
            screen.dial_form.settle()
        assert screen.landscape and screen.clock.compact_readout
        window.resize(QSize(window.minimumWidth(), window.minimumWidth()))
        assert not screen.landscape
        assert screen.left_layout.geometry().isEmpty()
        screen.dial_form.settle()
        assert not screen.clock.compact_readout
        _process(app, 12)
        assert not screen.clock.compact_readout


@pytest.mark.parametrize("locale", LOCALES)
def test_compact_controls_keep_their_width_when_the_window_grows(
    app, tmp_path, locale
):
    # The controls take the least width at which every action stays on the
    # page; the readout and the task picker get the rest.
    with (
        _scaled_font(app, BASE_POINTS * 1.5),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        assert screen.clock.compact_readout
        controls, readout = screen.controls_scroll.width(), screen.clock.width()
        more = screen.dial_side_width_for(True)
        window.resize(window.width() + more, window.height())
        _process(app, 12)
        assert screen.clock.compact_readout
        assert screen.controls_scroll.width() == controls
        assert screen.clock.width() == readout + more
        _assert_stop_sound_on_the_page(window)


def _shows_round_dial_at(app, window, width: int) -> bool:
    window.resize(width, window.height())
    _process(app, 12)
    return not window.timer_screen.clock.compact_readout


def _least_round_dial_width(app, window) -> int:
    """Least window width that shows the round dial at this height and font."""
    compact, round_dial = window.width(), window.width()
    assert not _shows_round_dial_at(app, window, compact)
    while not _shows_round_dial_at(app, window, round_dial):
        compact, round_dial = round_dial, round_dial * 2
    while round_dial - compact > 1:
        middle = (compact + round_dial) // 2
        if _shows_round_dial_at(app, window, middle):
            round_dial = middle
        else:
            compact = middle
    return round_dial


@pytest.mark.parametrize("locale", LOCALES)
def test_round_dial_returns_without_a_scroll_bar_when_the_window_grows(
    app, tmp_path, locale
):
    # A bar left over from a smaller page narrows the column, and Qt keeps
    # it while the narrowed column needs it. The round dial comes back only
    # at a width where the controls fit even then, so no bar stays.
    with (
        _scaled_font(app, BASE_POINTS * 1.5),
        _timer_window(app, tmp_path, locale, "default") as window,
    ):
        screen = window.timer_screen
        _show_state(app, window, SOUND_STATES["running_sound"])
        least = _least_round_dial_width(app, window)
        for width in (least - 1, least, least - 1, least):
            assert _shows_round_dial_at(app, window, width) == (width == least)
            for label, state in SOUND_STATES.items():
                _show_state(app, window, state)
                assert screen.controls_scroll.verticalScrollBar().maximum() == 0, label
                _assert_stop_sound_on_the_page(window)


@pytest.mark.parametrize("locale", LOCALES)
def test_fit_measures_every_label_whatever_the_timer_shows(app, tmp_path, locale):
    # The choice of the dial's form must not change with the timer state.
    with _timer_window(app, tmp_path, locale, "default") as window:
        screen = window.timer_screen
        budget = screen.button_budget_beside(screen.dial_side_width_for(False))
        measured = []
        for state in ALL_STATES.values():
            _show_state(app, window, state)
            measured.append(screen.dial_form._control_sizes(budget))
        assert all(sizes == measured[0] for sizes in measured)
        assert len(measured[0].primaries) == len(primary_labels(screen.strings))


def test_readout_side_is_never_narrower_than_the_readout(app, tmp_path):
    # With the shipped strings the task picker is the wider of the two. The
    # side must also hold a readout that is wider than the picker.
    with _timer_window(app, tmp_path, "en", "default") as window:
        screen = window.timer_screen
        readout = screen.clock.readout_minimum().width()
        assert readout > 0
        with patch.object(task_picker_row, "row_width", return_value=0):
            assert screen.dial_side_width_for(True) == readout


def _track_columns(image: QImage) -> list[int]:
    """Columns of the image that hold the countdown board's colour."""
    board = QColor(TRACK).rgb()
    return [
        x
        for x in range(image.width())
        if any(image.pixel(x, y) == board for y in range(image.height()))
    ]


def _painted(face: ReadoutFace, size: QSize, palette) -> QImage:
    image = QImage(size, QImage.Format.Format_RGB32)
    image.fill(palette.color(palette.ColorRole.Window))
    painter = QPainter(image)
    face.paint(painter, QRectF(0, 0, size.width(), size.height()), palette)
    painter.end()
    return image


def _state(status_text: str = "Running") -> ReadoutState:
    return ReadoutState("25:00", "Focus", status_text, 0.5, 2)


def test_readout_size_follows_the_font_and_not_the_labels(app):
    font = QFont(app.font())
    left = Qt.LayoutDirection.LeftToRight
    base = ReadoutFace(font, _state(), left).natural_size()
    long_status = ReadoutFace(font, _state("Running " * 12), left).natural_size()
    assert long_status == base
    doubled = QFont(font)
    doubled.setPointSizeF(font.pointSizeF() * 2)
    large = ReadoutFace(doubled, _state(), left).natural_size()
    assert large.width() > base.width() * 1.5
    assert large.height() > base.height() * 1.5


@pytest.mark.parametrize("share", [1.0, LEAST_SCALE])
def test_readout_paints_the_board_at_the_leading_side(app, share):
    palette = app.palette()
    font = QFont(app.font())
    size = ReadoutFace(font, _state(), Qt.LayoutDirection.LeftToRight).natural_size()
    # Twice the natural width leaves the trailing half empty.
    panel = QSize(round(size.width() * 2 * share), round(size.height() * share))
    middle = panel.width() // 2
    left = _track_columns(
        _painted(ReadoutFace(font, _state(), Qt.LayoutDirection.LeftToRight), panel, palette)
    )
    right = _track_columns(
        _painted(ReadoutFace(font, _state(), Qt.LayoutDirection.RightToLeft), panel, palette)
    )
    assert left and right
    assert max(left) < middle < min(right)
    # Both boards have one width. The trailing board starts where the
    # scaled width ends, between two pixels at some fonts, so its painted
    # columns may differ by the one that rounding gives or takes.
    assert abs(len(left) - len(right)) <= 1


def test_clock_asks_for_the_readout_size_only_in_the_compact_form(app):
    clock = ClockWidget()
    try:
        assert clock.minimumSize() == ClockWidget.round_minimum()
        round_hint = clock.sizeHint()
        clock.set_compact_readout(True)
        # The readout's least size comes from its content, not from the
        # round dial's minimum.
        assert clock.minimumSize().isNull()
        assert clock.minimumSizeHint() == clock.readout_minimum()
        natural = clock.sizeHint()
        assert natural.width() >= clock.readout_minimum().width()
        assert natural.height() >= clock.readout_minimum().height()
        image = QImage(natural, QImage.Format.Format_RGB32)
        clock.resize(natural)
        clock.render(image)
        assert _track_columns(image)
        clock.set_compact_readout(False)
        assert clock.minimumSize() == ClockWidget.round_minimum()
        assert clock.sizeHint() == round_hint
    finally:
        clock.deleteLater()
