"""Box layout that stacks its items when they do not fit side by side.

DSK-13: at large fonts a row of a label and controls can be wider than the
page. A scroll page then scrolls sideways and hides the start of every line.
The page measures the width it can give the row and hands it over; while the
items fit they sit in one row, otherwise they stack top to bottom.

The decision uses the items' minimum widths, which do not depend on the
current direction, so a row never flips back and forth at one width.
"""

from __future__ import annotations

from PySide6.QtWidgets import QBoxLayout

_ROW = QBoxLayout.Direction.LeftToRight
_STACK = QBoxLayout.Direction.TopToBottom


class ReflowRow(QBoxLayout):
    def __init__(self) -> None:
        super().__init__(_ROW)

    def row_width(self) -> int:
        """Narrowest width that keeps every item in one row."""
        widths = [
            self.itemAt(index).minimumSize().width()
            for index in range(self.count())
            if self.itemAt(index).spacerItem() is None
        ]
        margins = self.contentsMargins()
        gaps = max(len(widths) - 1, 0) * max(self.spacing(), 0)
        return sum(widths) + gaps + margins.left() + margins.right()

    def fit(self, width: int) -> None:
        """Lay the items out in a row when width holds them, else stack them."""
        direction = _ROW if self.row_width() <= width else _STACK
        if direction != self.direction():
            self.setDirection(direction)
