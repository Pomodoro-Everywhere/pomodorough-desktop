"""Flow of the Timer secondary actions (UX round 3 DSK-05, DSK-22, DSK-24).

The secondary actions (finish, cancel, skip, stop sound) used to share one
row in equal parts. A row whose buttons did not fit either widened the
controls column, which took width from the dial (DSK-05), or cut each
button to a share narrower than its longest word (DSK-22, DSK-24).

This layout keeps the buttons in their order and starts a new line when the
next button does not fit beside the previous ones at its size hint. Each
line splits its width as evenly as the buttons' hints allow, like a row of
equal stretch factors. The layout asks only for its widest button, so the
column width follows the page and the dial instead of the button count.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QStyle, QWidget


def pack_lines(widths: list[int], room: int, spacing: int) -> list[list[int]]:
    """Indices of each line when items of these widths flow into room.

    An item never shares a line when it would push the line past room; an
    item wider than room keeps a line of its own.
    """
    lines: list[list[int]] = []
    used = 0
    for index, width in enumerate(widths):
        if lines and used + spacing + width <= room:
            lines[-1].append(index)
            used += spacing + width
            continue
        lines.append([index])
        used = width
    return lines


def fill_line(widths: list[int], room: int, spacing: int) -> list[int]:
    """Widths that split room evenly without cutting any item below its width."""
    left = room - spacing * (len(widths) - 1)
    order = sorted(range(len(widths)), key=lambda index: widths[index], reverse=True)
    filled = list(widths)
    for position, index in enumerate(order):
        # Widest first: an item wider than the even share keeps its width,
        # and the rest split what remains.
        share = left // (len(order) - position)
        filled[index] = max(widths[index], share)
        left -= filled[index]
    return filled


def flowed_height(sizes: list[tuple[int, int]], room: int, spacing: int) -> int:
    """Height of items of these (width, height) sizes when they flow into room."""
    lines = pack_lines([width for width, _ in sizes], room, spacing)
    heights = [max(sizes[index][1] for index in line) for line in lines]
    return sum(heights) + spacing * max(0, len(lines) - 1)


class TimerActionFlow(QLayout):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), place=False)

    def sizeHint(self) -> QSize:
        # One line when the room allows it; the column decides the width.
        shown = self._shown()
        width = sum(item.sizeHint().width() for item in shown)
        width += self.spacing() * max(0, len(shown) - 1)
        return self._with_margins(QSize(width, self._tallest(shown)))

    def minimumSize(self) -> QSize:
        shown = self._shown()
        width = max((item.minimumSize().width() for item in shown), default=0)
        return self._with_margins(QSize(width, self._tallest(shown)))

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def _shown(self) -> list[QLayoutItem]:
        return [item for item in self._items if not item.isEmpty()]

    @staticmethod
    def _tallest(items: list[QLayoutItem]) -> int:
        return max((item.sizeHint().height() for item in items), default=0)

    def _with_margins(self, size: QSize) -> QSize:
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _arrange(self, rect: QRect, *, place: bool) -> int:
        """Lay the shown items out in rect; return the height they need."""
        margins = self.contentsMargins()
        inner = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        shown = self._shown()
        spacing = self.spacing()
        top = inner.top()
        for line in pack_lines(
            [item.sizeHint().width() for item in shown], inner.width(), spacing
        ):
            items = [shown[index] for index in line]
            height = self._tallest(items)
            if place:
                self._place_line(items, QRect(inner.left(), top, inner.width(), height))
            top += height + spacing
        if shown:
            top -= spacing
        return top - inner.top() + margins.top() + margins.bottom()

    def _place_line(self, items: list[QLayoutItem], line: QRect) -> None:
        spacing = self.spacing()
        widths = fill_line(
            [item.sizeHint().width() for item in items], line.width(), spacing
        )
        direction = Qt.LayoutDirection.LeftToRight
        parent = self.parentWidget()
        if parent is not None:
            direction = parent.layoutDirection()
        left = line.left()
        for item, width in zip(items, widths):
            cell = QRect(QPoint(left, line.top()), QSize(width, line.height()))
            item.setGeometry(QStyle.visualRect(direction, line, cell))
            left += width + spacing
