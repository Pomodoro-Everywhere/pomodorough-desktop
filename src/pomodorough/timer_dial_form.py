"""Shows the round dial or the compact readout on the Timer screen (DSK-12).

The round dial keeps its size at every font. At large fonts the controls
beside it are taller than a wide window's page, so they scrolled and Stop
Sound fell below the fold. Like the Apple clients at accessibility text
sizes, the screen then shows the compact readout. The task picker moves
under the readout, so the controls column holds only the buttons, and the
column gets the width at which its actions fit.

The choice comes from timer_dial_fit on measured sizes: the page, the
round dial's minimum, and the size hints of the picker and the buttons.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QSize, QTimer

from . import task_picker_row
from .task_picker_row import PickerFit
from .timer_action_labels import primary_labels, skip_labels
from .timer_dial_fit import (
    ColumnChrome,
    ControlSizes,
    Size,
    compact_keeps_primary_whole,
    controls_beside_round_dial,
    least_fitting_room,
    tallest_column,
    tallest_column_below_leading_rows,
)

if TYPE_CHECKING:
    from .timer_screen import TimerScreen


def _size(size: QSize) -> Size:
    return (size.width(), size.height())


class TimerDialForm(QObject):
    def __init__(self, screen: TimerScreen) -> None:
        super().__init__(screen)
        self._screen = screen
        # Resize and font events arrive inside Qt event propagation, where
        # moving a widget to another layout is not safe. The switch waits
        # for the event loop, like the settings reflow.
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self.settle)

    def schedule(self) -> None:
        if not self._settle_timer.isActive():
            self._settle_timer.start()

    def settle(self) -> None:
        """Show the form that fits the page now."""
        screen = self._screen
        page = screen.left_layout.geometry().size()
        if page.isEmpty():
            # An invalidated layout forgets its geometry until Qt lays it
            # out again, and the event loop can run this before that
            # layout request. Lay the page out now so the form follows a
            # change of orientation instead of waiting for the next resize.
            screen.left_layout.activate()
            page = screen.left_layout.geometry().size()
        if page.isEmpty():
            # Not laid out yet: there is nothing to measure against.
            return
        compact = self.compact_readout_suits(page)
        if compact != screen.clock.compact_readout:
            screen.clock.set_compact_readout(compact)
            self._arrange(compact)
        if not compact:
            screen.follow_dial_form(None)
            return
        room, column = self._compact_fit(page, self.compact_side_width())
        # Measured on the page read above: the changes below invalidate
        # the layout, which then forgets its geometry until Qt lays the
        # page out again.
        picker = self._picker_under_readout(page, room)
        screen.follow_dial_form(self._controls_width(room), column - page.height())
        task_picker_row.set_stacked(screen.task_selector_panel, picker.stacked)

    def _controls_width(self, room: int) -> int:
        """Width of the controls whose content is room wide."""
        bar = self._screen.controls_scroll.bar_allowance()
        return room + self._column_chrome().width + bar

    def _picker_under_readout(self, page: QSize, controls_room: int) -> PickerFit:
        """How the task picker sits under the compact readout beside
        controls whose content is controls_room wide.

        The title is the no-task choice, which the picker always holds:
        the form of the dial must not change with the selected task.
        """
        screen = self._screen
        beside = self._controls_width(controls_room)
        side = page.width() - screen.left_layout.spacing() - beside
        return task_picker_row.fit_under_readout(
            screen.task_selector_panel,
            side,
            screen.room_below_readout(page.height()),
            screen.strings.text("task.none"),
        )

    def compact_readout_suits(self, page: QSize) -> bool:
        """Whether this page shows the compact readout, not the round dial."""
        screen = self._screen
        if not screen.landscape:
            # In a tall window the dial sits above the controls. The
            # readout's height follows the font, and at the fonts where
            # the dial stops fitting it is about as tall as the dial's
            # minimum, so the swap would not bring Stop Sound back.
            return False
        round_dial = self._round_dial_column(page)
        if tallest_column(*round_dial) <= page.height():
            return False
        return self._compact_beats_round_dial(page, self.compact_side_width())

    def _compact_beats_round_dial(self, page: QSize, side: int) -> bool:
        """Whether a compact readout with a side this wide serves the page
        better than the round dial, which does not fit it."""
        screen = self._screen
        round_dial = self._round_dial_column(page)
        beside_round_dial = round_dial[1]
        beside_readout = self._control_sizes(screen.button_budget_beside(side))
        if not compact_keeps_primary_whole(
            page.height(), beside_readout, beside_round_dial
        ):
            return False
        room, compact_column = self._compact_fit(page, side)
        picker = self._picker_under_readout(page, room)
        holds = picker.holds(screen.room_below_readout(page.height()))
        overflow = compact_column - page.height()
        if overflow <= 0 or screen.margins_absorb(overflow):
            # R55-D01: the readout's side must hold what moves there. A
            # font with a taller line leaves the readout less than its
            # least height above the picker, or cuts the task title.
            # The trimmed margins take up the overflow, so nothing scrolls
            # and the side must hold the readout and the whole title.
            return holds
        if not holds and not picker.stacked:
            # The label beside the picker cuts the title or takes the
            # readout's least height, and stacking does not suit the page.
            return False
        # Both forms scroll. The swap must then bring Stop Sound higher;
        # beside the round dial the task picker can scroll out of view
        # above the primary action.
        return compact_column < tallest_column_below_leading_rows(*round_dial)

    def _round_dial_column(self, page: QSize) -> tuple[ColumnChrome, ControlSizes, int]:
        """Chrome, sizes and content width of the controls column beside
        the round dial, with the task picker on top."""
        screen = self._screen
        dial = screen.dial_side_width_for(False)
        buttons = self._control_sizes(screen.button_budget_beside(dial))
        side_by_side = page.width() - screen.left_layout.spacing()
        chrome = self._column_chrome()
        # The controls must fit with the bar shown: a bar left over from a
        # smaller page narrows the column, and Qt keeps it while the
        # narrowed column still needs it.
        bar = screen.controls_scroll.bar_allowance()
        room = controls_beside_round_dial(side_by_side, dial) - chrome.width - bar
        return chrome, replace(buttons, leading_rows=[self._picker_size(room)]), room

    def compact_side_width(self) -> int:
        """Least width of the dial's side under the compact readout.

        The task picker sits under the readout. Its width counts from the
        moment it moves, before the layout shows it there. The picker's
        label stays beside it while the controls then fit the page, so
        the title keeps the width. When the controls fit only with the
        label above the picker, Stop Sound wins. When they scroll either
        way, the title keeps the width while the compact readout still
        beats the round dial with it; a few pixels of Stop Sound below the
        fold then cost less than a cut title.
        """
        screen = self._screen
        panel = screen.task_selector_panel
        readout = screen.clock.readout_minimum().width()
        row = max(readout, task_picker_row.row_width(panel, preferred=False))
        stacked = max(readout, task_picker_row.stacked_width(panel))
        page = screen.left_layout.geometry().size()
        room = screen.room_below_readout(page.height())
        if stacked >= row or not task_picker_row.suits_stacking(panel, stacked, room):
            return row
        if self._compact_fit(page, row)[1] <= page.height():
            return row
        if self._compact_fit(page, stacked)[1] <= page.height():
            return stacked
        if self._compact_beats_round_dial(page, row):
            return row
        return stacked

    def _compact_fit(self, page: QSize, side: int) -> tuple[int, int]:
        """Content width and tallest height of the controls column beside
        a compact readout's side this wide.

        The width is the least at which every action stays on the page;
        the readout and the task picker get the rest.
        """
        screen = self._screen
        chrome = self._column_chrome()
        bar = screen.controls_scroll.bar_allowance()
        sizes = self._control_sizes(screen.button_budget_beside(side))
        beside = page.width() - screen.left_layout.spacing() - side
        most_room = beside - chrome.width - bar
        room = least_fitting_room(chrome, sizes, page.height(), most_room)
        return room, tallest_column(chrome, sizes, room)

    def _control_sizes(self, budget: int) -> ControlSizes:
        """Sizes of every label the buttons can show in this width budget."""
        screen = self._screen
        primaries = [
            _size(screen.primary_button.hint_for_label(label, budget))
            for label in primary_labels(screen.strings)
        ]
        finish, cancel, stop = (
            _size(button.hint_for_label(button.text(), budget))
            for button in (
                screen.finish_button,
                screen.cancel_button,
                screen.stop_sound_button,
            )
        )
        skips = [
            _size(screen.skip_button.hint_for_label(label, budget))
            for label in skip_labels(screen.strings)
        ]
        # Stop Sound counts in every set: it shows whenever a sound plays.
        action_sets = [[finish, cancel, stop], *([skip, stop] for skip in skips)]
        return ControlSizes([], primaries, action_sets)

    def _column_chrome(self) -> ColumnChrome:
        column = self._screen.controls_column
        # The full margins: the fit decides how far the screen trims them.
        margins = self._screen.full_column_margins()
        frame = column.frameWidth() * 2
        return ColumnChrome(
            width=margins.left() + margins.right() + frame,
            height=margins.top() + margins.bottom() + frame,
            row_spacing=column.layout().spacing(),
            action_spacing=self._screen.secondary_actions.spacing(),
        )

    def _picker_size(self, room: int) -> Size:
        """The picker row in a column whose content is room wide."""
        picker = self._screen.task_selector_panel
        width = picker.minimumSizeHint().width()
        height = picker.sizeHint().height()
        if picker.hasHeightForWidth():
            # Its label wraps onto more lines in a narrow column.
            height = max(height, picker.heightForWidth(max(room, width)))
        return (width, height)

    def _arrange(self, compact: bool) -> None:
        screen = self._screen
        readout = screen.readout_layout
        picker = screen.task_selector_panel
        column = screen.controls_column.layout()
        while readout.count():
            readout.takeAt(0)
        if not compact:
            task_picker_row.set_stacked(picker, False)
            readout.addWidget(screen.clock, 1)
            column.insertWidget(0, picker)
            return
        # The readout and the picker sit together in the middle of their
        # side; each keeps the height its content asks for.
        column.removeWidget(picker)
        readout.addStretch(1)
        readout.addWidget(screen.clock)
        readout.addWidget(picker)
        readout.addStretch(1)
