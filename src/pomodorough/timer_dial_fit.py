"""Whether the round dial and the Timer controls fit the page (DSK-12).

At large fonts the controls column is taller than the page, so it scrolled
and Stop Sound fell below the fold. The Timer screen then shows the compact
readout instead of the round dial.

The rules here work on measured sizes only and never on a font threshold.
They measure the controls with every label the buttons can show and with
Stop Sound shown. The answer therefore depends on the window, the font and
the language, and not on the form the dial has now or on the timer state:
the dial does not change its form when a timer starts or a sound plays.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .timer_action_flow import flowed_height

Size = tuple[int, int]

# Shares of the page width beside each other in a wide window: the round
# dial takes three parts, the controls two.
ROUND_DIAL_SHARES = (3, 2)


@dataclass(frozen=True)
class ColumnChrome:
    """Frame, margins and gaps of the controls column."""

    width: int
    height: int
    row_spacing: int
    action_spacing: int


@dataclass(frozen=True)
class ControlSizes:
    """Measured sizes of everything the controls column can show.

    leading_rows are shown above the primary action in every state. The
    column shows one primary label and one action set at a time.
    """

    leading_rows: list[Size]
    primaries: list[Size]
    action_sets: list[list[Size]]

    def tallest_primary(self) -> int:
        return max(height for _, height in self.primaries)

    def narrowest_room(self) -> int:
        """The column is never narrower than its widest item."""
        actions = [size for actions in self.action_sets for size in actions]
        sizes = (*self.leading_rows, *self.primaries, *actions)
        return max(width for width, _ in sizes)


def controls_beside_round_dial(side_by_side: int, dial_minimum: int) -> int:
    """Width the controls get beside the round dial.

    side_by_side is the page width without the gap between the two. The
    controls take their share, and never the dial's minimum width.
    """
    dial_share, controls_share = ROUND_DIAL_SHARES
    share = side_by_side * controls_share // (dial_share + controls_share)
    return min(share, side_by_side - dial_minimum)


def column_height(
    chrome: ColumnChrome, rows: list[Size], actions: list[Size], room: int
) -> int:
    """Height of the controls column when its content gets room in width.

    rows sit above the actions, one per line. The actions flow onto the
    fewest lines the width allows.
    """
    width = max(room, *(width for width, _ in (*rows, *actions)))
    heights = [height for _, height in rows]
    heights.append(flowed_height(actions, width, chrome.action_spacing))
    return chrome.height + sum(heights) + chrome.row_spacing * (len(heights) - 1)


def tallest_column(chrome: ColumnChrome, sizes: ControlSizes, room: int) -> int:
    """The tallest column over every primary label and every action set."""
    return max(
        column_height(chrome, [*sizes.leading_rows, primary], actions, room)
        for primary in sizes.primaries
        for actions in sizes.action_sets
    )


def tallest_column_below_leading_rows(
    chrome: ColumnChrome, sizes: ControlSizes, room: int
) -> int:
    """The tallest column without its leading rows, at their width.

    The controls scroll keeps the primary action whole, so the rows above
    it can scroll out of view. Stop Sound shows when the rest of the column
    fits the page.
    """
    width = max(room, *(width for width, _ in sizes.leading_rows))
    return tallest_column(chrome, replace(sizes, leading_rows=[]), width)


def compact_keeps_primary_whole(
    page_height: int, beside_readout: ControlSizes, beside_round_dial: ControlSizes
) -> bool:
    """Whether the buttons beside the compact readout keep the primary whole.

    The task picker sits under the readout and needs more width than the
    round dial's minimum, so the buttons get less and their labels wrap
    onto more lines. The primary action must not grow taller than the page
    from that; the round dial stays when it would.
    """
    limit = max(page_height, beside_round_dial.tallest_primary())
    return beside_readout.tallest_primary() <= limit


def _line_break_rooms(sizes: ControlSizes, spacing: int) -> list[int]:
    """Every width at which an action line can hold one more action."""
    rooms = {sizes.narrowest_room()}
    for actions in sizes.action_sets:
        widths = [width for width, _ in actions]
        for start in range(len(widths)):
            run = widths[start]
            for width in widths[start + 1 :]:
                run += spacing + width
                rooms.add(run)
    return sorted(room for room in rooms if room >= sizes.narrowest_room())


def least_fitting_room(
    chrome: ColumnChrome, sizes: ControlSizes, page_height: int, most_room: int
) -> int:
    """Least content width at which the column fits the page height.

    The compact readout gets the rest of the page width. When no width up
    to most_room fits, the answer is the width with the shortest column,
    so the column scrolls the least. The answer is never above most_room:
    the page has no more to give.
    """
    rooms = _line_break_rooms(sizes, chrome.action_spacing)
    rooms = [room for room in rooms if room <= most_room] or [max(most_room, 0)]
    heights = {room: tallest_column(chrome, sizes, room) for room in rooms}
    for room in rooms:
        if heights[room] <= page_height:
            return room
    return min(rooms, key=lambda room: (heights[room], room))
