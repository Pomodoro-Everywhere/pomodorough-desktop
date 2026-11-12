"""Scroll wrapper that keeps the Timer primary action whole (UX round 2 B1).

At large fonts in a short window, the controls column (task picker, primary
action, secondary row) needs more height than the page can give. A layout
squeezed below its minimum overlaps its children, so the secondary row used
to cover the primary button. The column now scrolls inside this wrapper.
The wrapper asks for only the height the primary action needs, so the dial
and the primary action keep priority; the picker and the secondary row
scroll. Whenever the wrapper or the column changes size, the primary action
is scrolled back into full view.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QStyle, QWidget


class TimerControlsScroll(QScrollArea):
    # The column's measured minimum changed (new label or font), so width
    # caps derived from it must be recomputed.
    column_hints_changed = Signal()

    def __init__(self, column: QWidget, primary: QWidget) -> None:
        super().__init__()
        self._column = column
        self._primary = primary
        self.setObjectName("timerControlsScroll")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        # B2 L3: the vertical bar shows on overflow, so a pointer without a
        # wheel or trackpad can still reach the picker and the secondary row.
        # minimumSizeHint reserves the bar's measured width, so a shown bar
        # never narrows the column below its minimum; the horizontal bar
        # therefore never has anything to scroll and stays off.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setWidget(column)
        column.installEventFilter(self)
        primary.installEventFilter(self)

    def minimumSizeHint(self) -> QSize:
        # Width: the whole column, as before the wrapper existed. Height:
        # only the primary action inside the column margins.
        margins = self._column.contentsMargins()
        layout = self._column.layout()
        if layout is not None:
            margins = layout.contentsMargins()
        primary = self._primary.minimumSizeHint().expandedTo(
            self._primary.minimumSize()
        )
        return QSize(
            self._column.minimumSizeHint().width() + self.bar_allowance(),
            primary.height() + margins.top() + margins.bottom(),
        )

    def bar_allowance(self) -> int:
        """Width a shown vertical bar takes from the column; 0 when it overlays."""
        if self.style().styleHint(QStyle.StyleHint.SH_ScrollBar_Transient, None, self):
            return 0
        return self.verticalScrollBar().sizeHint().width()

    def sizeHint(self) -> QSize:
        return self._column.sizeHint().expandedTo(self.minimumSizeHint())

    def keep_primary_whole(self) -> None:
        """Scroll the least distance that shows the whole primary action."""
        self.ensureWidgetVisible(self._primary, 0, 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.keep_primary_whole()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Scrolling moves the column, not the primary action inside it, so
        # a user scroll toward the secondary row is never snapped back.
        relayout = (QEvent.Type.Resize,)
        if watched is self._primary:
            relayout = (QEvent.Type.Resize, QEvent.Type.Move)
        if event.type() in relayout:
            self.keep_primary_whole()
        elif watched is self._column and event.type() == QEvent.Type.LayoutRequest:
            # A new label or font changes the column's hints.
            self.updateGeometry()
            self.column_hints_changed.emit()
        return super().eventFilter(watched, event)
