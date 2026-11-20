"""UX round 3 DSK-12: the rules that choose the round dial or the readout.

timer_dial_fit works on measured sizes only. These tests give it sizes by
hand, so each rule is checked at its exact boundary, and then compare its
predictions with what the real widgets measure.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)
from test_ux_timer_controls import SETTINGS, _render

from pomodorough.core import PHASES
from pomodorough.localization import Strings
from pomodorough.timer_action_flow import TimerActionFlow, flowed_height
from pomodorough.timer_action_labels import primary_labels, skip_labels
from pomodorough.timer_dial_fit import (
    ROUND_DIAL_SHARES,
    ColumnChrome,
    ControlSizes,
    column_height,
    compact_keeps_primary_whole,
    controls_beside_round_dial,
    least_fitting_room,
    tallest_column,
    tallest_column_below_leading_rows,
)
from pomodorough.timer_screen import TimerScreen
from pomodorough.wrapping_push_button import WrappingPushButton

CHROME = ColumnChrome(width=12, height=10, row_spacing=4, action_spacing=6)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def test_controls_take_their_share_beside_the_round_dial():
    dial_share, controls_share = ROUND_DIAL_SHARES
    parts = dial_share + controls_share
    assert controls_beside_round_dial(100 * parts, 170) == 100 * controls_share


def test_controls_leave_the_round_dial_its_minimum_width():
    # The share would be 120 and would leave the dial 180. One more pixel
    # of dial minimum takes it from the controls.
    assert controls_beside_round_dial(300, 180) == 120
    assert controls_beside_round_dial(300, 181) == 119


def test_column_height_sums_rows_gaps_and_flowed_actions():
    rows = [(50, 20), (60, 30)]
    actions = [(40, 10), (40, 12)]
    # 40 + 6 + 40 fits 86 exactly: one action line, as tall as its tallest.
    assert column_height(CHROME, rows, actions, 86) == 10 + 20 + 30 + 12 + 2 * 4
    # One pixel less starts a second line, with the action gap between.
    assert column_height(CHROME, rows, actions, 85) == 10 + 20 + 30 + (10 + 6 + 12) + 2 * 4


def test_column_is_never_narrower_than_its_widest_item():
    rows = [(90, 20)]
    actions = [(40, 10), (40, 10)]
    # Room 10 would put each action on its own line. The 90 wide row
    # keeps the column at 90, where both share a line.
    assert column_height(CHROME, rows, actions, 10) == column_height(
        CHROME, rows, actions, 90
    )


def test_tallest_column_covers_every_primary_and_every_action_set():
    sizes = ControlSizes(
        leading_rows=[(50, 20)],
        primaries=[(60, 30), (60, 44)],
        action_sets=[[(40, 10)], [(40, 10), (70, 16)]],
    )
    # Room 100 cannot hold 40 + 6 + 70, so the second set takes two lines.
    expected = 10 + 20 + 44 + (10 + 6 + 16) + 2 * 4
    assert tallest_column(CHROME, sizes, 100) == expected
    assert tallest_column(CHROME, sizes, 116) == 10 + 20 + 44 + 16 + 2 * 4


def _buttons(primary_height: int) -> ControlSizes:
    return ControlSizes([], [(60, 20), (60, primary_height)], [[(40, 10)]])


def test_compact_form_is_refused_only_when_it_makes_the_primary_taller_than_the_page():
    beside_round_dial = _buttons(40)
    assert compact_keeps_primary_whole(100, _buttons(100), beside_round_dial)
    assert not compact_keeps_primary_whole(100, _buttons(101), beside_round_dial)
    # A primary already taller than the page beside the round dial is not
    # made worse by the readout.
    assert compact_keeps_primary_whole(100, _buttons(120), _buttons(120))
    assert not compact_keeps_primary_whole(100, _buttons(121), _buttons(120))


def _actions() -> ControlSizes:
    # One primary, and three actions: 40 + 6 + 50 + 6 + 30 on one line, and
    # 40 over 50 + 6 + 30 on two.
    return ControlSizes([], [(80, 20)], [[(40, 10), (50, 10), (30, 10)]])


def test_column_below_the_picker_keeps_the_picker_width():
    # The picker above the primary can scroll out of view; what stays is
    # the rest of the column, which is as wide as the picker.
    sizes = ControlSizes([(200, 30)], *_actions_beside_primary())
    one_line = CHROME.height + 20 + CHROME.row_spacing + 10
    assert tallest_column_below_leading_rows(CHROME, sizes, 60) == one_line
    assert tallest_column(CHROME, sizes, 60) == one_line + 30 + CHROME.row_spacing
    unpicked = ControlSizes([], *_actions_beside_primary())
    assert tallest_column(CHROME, unpicked, 60) > one_line


def _actions_beside_primary() -> tuple[list, list]:
    return _actions().primaries, _actions().action_sets


def test_least_fitting_room_is_the_least_width_whose_column_fits_the_page():
    sizes = _actions()
    one_line = 10 + 20 + 10 + 4
    two_lines = one_line + 6 + 10
    three_lines = two_lines + 6 + 10
    assert tallest_column(CHROME, sizes, 132) == one_line
    assert tallest_column(CHROME, sizes, 131) == two_lines
    assert tallest_column(CHROME, sizes, 86) == two_lines
    assert tallest_column(CHROME, sizes, 85) == three_lines
    # The column is 80 wide at least, the primary's width.
    assert least_fitting_room(CHROME, sizes, three_lines, 400) == 80
    assert least_fitting_room(CHROME, sizes, two_lines, 400) == 86
    assert least_fitting_room(CHROME, sizes, one_line, 400) == 132
    assert least_fitting_room(CHROME, sizes, one_line, 132) == 132


def test_least_fitting_room_takes_the_shortest_column_when_nothing_fits():
    sizes = _actions()
    one_line = 10 + 20 + 10 + 4
    # No width makes the column shorter than one line of actions.
    assert least_fitting_room(CHROME, sizes, one_line - 1, 400) == 132
    # The page cannot give 132, so two lines are the shortest it can show.
    assert least_fitting_room(CHROME, sizes, one_line, 131) == 86


def test_least_fitting_room_never_exceeds_the_room_the_page_has():
    sizes = _actions()
    assert least_fitting_room(CHROME, sizes, 1000, 79) == 79
    assert least_fitting_room(CHROME, sizes, 0, 79) == 79
    assert least_fitting_room(CHROME, sizes, 0, -5) == 0


@pytest.mark.parametrize(
    "direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft]
)
def test_flowed_height_matches_the_action_flow(app, direction):
    host = QWidget()
    host.setLayoutDirection(direction)
    flow = TimerActionFlow(host)
    flow.setContentsMargins(0, 0, 0, 0)
    try:
        for label in ("Finish", "Cancel timer", "Stop\nsound"):
            flow.addWidget(QPushButton(label))
        hints = [flow.itemAt(index).sizeHint() for index in range(flow.count())]
        sizes = [(hint.width(), hint.height()) for hint in hints]
        widest = sum(width for width, _ in sizes) + 2 * flow.spacing()
        narrowest = max(width for width, _ in sizes)
        heights = set()
        for room in range(narrowest, widest + 2):
            predicted = flowed_height(sizes, room, flow.spacing())
            assert predicted == flow.heightForWidth(room), room
            heights.add(predicted)
        # The range covers one, two and three lines.
        assert len(heights) == 3
    finally:
        host.deleteLater()


def _shown_button(app, text: str) -> tuple[QWidget, WrappingPushButton]:
    host = QWidget()
    host.setStyleSheet(
        "QPushButton { border: 2px solid black; padding: 3px 7px; }"
        "QPushButton:focus { border: 3px solid black; }"
    )
    button = WrappingPushButton(text)
    QVBoxLayout(host).addWidget(button)
    host.show()
    app.processEvents()
    return host, button


def test_hint_for_label_is_the_size_the_button_asks_for(app):
    text = "Start long break now"
    host, button = _shown_button(app, text)
    try:
        resting = button.sizeHint()
        predicted = button.hint_for_label(text, None)
        assert predicted.width() >= resting.width()
        assert predicted.height() >= resting.height()
        # A label the button does not show: the button stays as it is.
        other = button.hint_for_label("Pause", None)
        assert other.width() < predicted.width()
        assert button.text() == text
        assert button.sizeHint() == resting
        button.setText("Pause")
        app.processEvents()
        assert button.sizeHint().width() <= other.width()
        assert button.sizeHint().height() <= other.height()
    finally:
        host.deleteLater()


def _focused_hint(button: WrappingPushButton):
    """The size the style gives the button's label with the focus ring."""
    option = QStyleOptionButton()
    button.initStyleOption(option)
    option.state |= QStyle.StateFlag.State_HasFocus
    content = button.fontMetrics().size(Qt.TextFlag.TextShowMnemonic, button.text())
    option.rect.setSize(content)
    return button.style().sizeFromContents(
        QStyle.ContentsType.CT_PushButton, option, content, button
    )


def test_hint_for_label_counts_the_focus_border(app):
    # The button caches the hint it measured with the focus ring, and that
    # one is larger. A prediction from the resting border alone would put
    # the column a few pixels short of the page.
    text = "Start long break now"
    host, button = _shown_button(app, text)
    try:
        focused = _focused_hint(button)
        assert focused.width() > button.sizeHint().width()
        assert button.hint_for_label(text, None) == focused
    finally:
        host.deleteLater()


def test_hint_for_label_wraps_the_label_into_the_width_budget(app):
    text = "Start long break now"
    host, button = _shown_button(app, text)
    try:
        one_line = button.hint_for_label(text, None)
        budget = one_line.width() * 2 // 3
        wrapped = button.hint_for_label(text, budget)
        assert wrapped.width() <= budget
        assert wrapped.height() > one_line.height()
        # The same answer as the button gives once it has that budget.
        button.set_width_budget(budget)
        app.processEvents()
        assert button.sizeHint().width() <= wrapped.width()
        assert button.sizeHint().height() <= wrapped.height()
        assert button.sizeHint().height() > one_line.height()
    finally:
        host.deleteLater()


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_measured_labels_cover_every_label_the_controls_show(app, locale):
    strings = Strings(locale)
    screen = TimerScreen(strings, SETTINGS)
    try:
        primaries, skips = set(), set()
        for status in ("idle", "running", "paused", "completed", "cancelled"):
            for phase in PHASES:
                for destination in (*PHASES, None):
                    _render(
                        screen,
                        status,
                        selected_phase=phase,
                        skip_destination=destination,
                    )
                    primaries.add(" ".join(screen.primary_button.text().split()))
                    skips.add(" ".join(screen.skip_button.text().split()))
        assert primaries <= {" ".join(label.split()) for label in primary_labels(strings)}
        assert skips <= {" ".join(label.split()) for label in skip_labels(strings)}
        # Each list holds no label the controls never show.
        assert len(primaries) == len(set(primary_labels(strings)))
        assert len(skips) == len(set(skip_labels(strings)))
    finally:
        screen.deleteLater()
