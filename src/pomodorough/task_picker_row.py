"""The "Focus task" label and the task picker as one row (DSK-12).

Under the compact readout the picker gets the width of the readout's side,
which is often less than the row asks for, so the picker cut the task
title. The label then goes above the picker, and the picker gets the whole
width of the side. Stacked, the two need more height, so they stack only
while the readout keeps its least height above them, or when the readout
lacks that height beside the row as well.

The widths and heights are measured from the panel's own layout, whatever
direction it has now, so a fit can ask for either arrangement.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QBoxLayout, QFrame, QLayoutItem


@dataclass(frozen=True)
class PickerFit:
    """How the panel sits under the compact readout in one side width."""

    stacked: bool
    height: int
    title_whole: bool

    def holds(self, room: int) -> bool:
        """Whether the readout keeps its least height above the panel and
        the picker shows the title without an ellipsis."""
        return self.height <= room and self.title_whole


def _items(panel: QFrame) -> list[QLayoutItem]:
    layout = panel.layout()
    return [layout.itemAt(index) for index in range(layout.count())]


def _chrome(panel: QFrame) -> tuple[int, int]:
    """Width and height of the panel's margins and frame."""
    margins = panel.layout().contentsMargins()
    frame = panel.frameWidth() * 2
    return (
        margins.left() + margins.right() + frame,
        margins.top() + margins.bottom() + frame,
    )


def row_width(panel: QFrame, *, preferred: bool) -> int:
    """Width of the label and the picker side by side.

    preferred: the width they ask for; otherwise the least they take.
    """
    sizes = [
        item.sizeHint() if preferred else item.minimumSize() for item in _items(panel)
    ]
    gaps = panel.layout().spacing() * (len(sizes) - 1)
    return sum(size.width() for size in sizes) + gaps + _chrome(panel)[0]


def row_height(panel: QFrame) -> int:
    """Height of the label and the picker side by side."""
    return max(item.sizeHint().height() for item in _items(panel)) + _chrome(panel)[1]


def stacked_height(panel: QFrame, width: int) -> int:
    """Height of the label above the picker in a panel this wide."""
    chrome_width, chrome_height = _chrome(panel)
    inner = width - chrome_width
    heights = [
        item.heightForWidth(inner)
        if item.hasHeightForWidth()
        else item.sizeHint().height()
        for item in _items(panel)
    ]
    gaps = panel.layout().spacing() * (len(heights) - 1)
    return sum(heights) + gaps + chrome_height


def _row_shares(panel: QFrame, width: int) -> tuple[int, int]:
    """Widths of the label and the picker side by side in a panel this wide.

    The picker stretches, so QBoxLayout counts it at its least width and
    gives the label the width it asks for first. The picker gets the
    rest. A panel too narrow for that takes the lack from the label,
    down to the label's least width.
    """
    label, picker = _items(panel)
    space = width - _chrome(panel)[0] - panel.layout().spacing()
    beside_least_picker = space - picker.minimumSize().width()
    label_width = min(label.sizeHint().width(), beside_least_picker)
    label_width = max(label.minimumSize().width(), label_width)
    return label_width, space - label_width


def _wrapped_row_height(panel: QFrame, width: int) -> int:
    """Height of the label beside the picker in a panel this wide. The
    label wraps onto more lines when the row gets less than it asks for."""
    heights = [
        item.heightForWidth(share)
        if item.hasHeightForWidth()
        else item.sizeHint().height()
        for item, share in zip(_items(panel), _row_shares(panel, width), strict=True)
    ]
    return max(heights) + _chrome(panel)[1]


def stacked_width(panel: QFrame) -> int:
    """Least width of the label above the picker."""
    return max(item.minimumSize().width() for item in _items(panel)) + _chrome(panel)[0]


def suits_stacking(panel: QFrame, width: int, room: int) -> bool:
    """Whether the label may go above the picker in a panel this wide.

    room: the height the readout leaves when it keeps its least height.
    """
    return stacked_height(panel, width) <= room or row_height(panel) > room


def fit_under_readout(panel: QFrame, width: int, room: int, title: str) -> PickerFit:
    """The arrangement, the height and the title of a panel this wide.

    room: the height the readout leaves when it keeps its least height.
    The label goes above the picker when the panel is narrower than the
    row asks for and stacking suits the room.
    """
    narrow = width < row_width(panel, preferred=True)
    stacked = narrow and suits_stacking(panel, width, room)
    if stacked:
        height = stacked_height(panel, width)
        picker_width = width - _chrome(panel)[0]
    else:
        height = _wrapped_row_height(panel, width)
        picker_width = _row_shares(panel, width)[1]
    picker = _items(panel)[1].widget()
    return PickerFit(stacked, height, picker.shows_whole(title, picker_width))


def set_stacked(panel: QFrame, stacked: bool) -> None:
    direction = QBoxLayout.Direction.LeftToRight
    if stacked:
        direction = QBoxLayout.Direction.TopToBottom
    if panel.layout().direction() != direction:
        panel.layout().setDirection(direction)
