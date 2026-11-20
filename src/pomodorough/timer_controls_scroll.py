"""Scroll wrapper that keeps the Timer primary action whole (UX round 2 B1).

At large fonts in a short window, the controls column (task picker, primary
action, secondary row) needs more height than the page can give. A layout
squeezed below its minimum overlaps its children, so the secondary row used
to cover the primary button. The column now scrolls inside this wrapper.
The wrapper asks for only the height the primary action needs, so the dial
and the primary action keep priority; the picker and the secondary row
scroll. Whenever the wrapper or the column changes size, the primary action
is scrolled back into full view.

DSK-12: while a sound plays, the same scroll also brings Stop Sound as far
into view as the whole primary action allows, so the action the user needs
first is not left below the fold by an earlier scroll position. When Stop
Sound hides, the scroll goes back up to the task picker, because picking
the next task usually comes after the sound.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QStyle, QWidget


class TimerControlsScroll(QScrollArea):
    # The column's measured minimum changed (new label or font), so width
    # caps derived from it must be recomputed.
    column_hints_changed = Signal()

    def __init__(
        self, column: QWidget, picker: QWidget, primary: QWidget, stop_sound: QWidget
    ) -> None:
        super().__init__()
        self._column = column
        self._picker = picker
        self._primary = primary
        self._stop_sound = stop_sound
        self._hinted_height = -1
        self._back_to_picker = False
        self.setObjectName("timerControlsScroll")
        # DSK-04: the page is no Tab stop; focus goes to its controls.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        stop_sound.installEventFilter(self)

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
        hint = self._column.sizeHint()
        # DSK-05: the secondary actions flow onto more lines when the column
        # is narrower than one row of them, so the preferred height follows
        # the column's measured width.
        flowed = self._flowed_height()
        if flowed > 0:
            hint.setHeight(flowed)
        # DSK-12: like the minimum, the preferred width reserves the bar. A
        # shown bar would otherwise narrow a column at its one-line width,
        # the actions would flow onto a second line, and that line would
        # keep the bar after the page has grown tall enough for one line.
        hint.setWidth(hint.width() + self.bar_allowance())
        return hint.expandedTo(self.minimumSizeHint())

    def _flowed_height(self) -> int:
        width = self._column.width()
        if width <= 0 or not self._column.hasHeightForWidth():
            return -1
        return self._column.heightForWidth(width)

    def _follow_flowed_height(self) -> None:
        # A new column width may move a secondary action to another line.
        # Ask the page for the new height only when it changed, so the
        # relayout that follows settles instead of repeating.
        flowed = self._flowed_height()
        if flowed != self._hinted_height:
            self._hinted_height = flowed
            self.updateGeometry()

    def keep_primary_whole(self) -> None:
        """Scroll the least distance that shows the whole primary action.

        While Stop Sound shows, scroll toward it first; just after it hides,
        scroll toward the task picker first. The primary action then wins
        wherever both do not fit the viewport. Under the compact readout
        the picker is outside the column, and Qt ignores the scroll to it.
        """
        if self._stop_sound.isVisibleTo(self._column):
            self.ensureWidgetVisible(self._stop_sound, 0, 0)
        elif self._back_to_picker:
            self.ensureWidgetVisible(self._picker, 0, 0)
        self.ensureWidgetVisible(self._primary, 0, 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.keep_primary_whole()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The scroll area sets its scroll range from the column's resize in
        # this same filter, so it runs first: a scroll toward the primary
        # action against the old range stops short of it.
        handled = super().eventFilter(watched, event)
        if watched is self._stop_sound:
            # Showing Stop Sound resizes the column, which scrolls toward it.
            # Hiding it starts a relayout whose passes can push the primary
            # action down for a moment, so the scroll toward the picker holds
            # until the column has its new size.
            if event.type() == QEvent.Type.HideToParent:
                self._back_to_picker = True
                self.keep_primary_whole()
            return handled
        # Scrolling moves the column, not the primary action inside it, so
        # a user scroll toward the secondary row is never snapped back.
        relayout = (QEvent.Type.Resize,)
        if watched is self._primary:
            relayout = (QEvent.Type.Resize, QEvent.Type.Move)
        if event.type() in relayout and watched is self._column:
            self._follow_flowed_height()
            self.keep_primary_whole()
            self._back_to_picker = False
        elif event.type() in relayout:
            self.keep_primary_whole()
        elif watched is self._column and event.type() == QEvent.Type.LayoutRequest:
            # A new label or font changes the column's hints.
            self.updateGeometry()
            self.column_hints_changed.emit()
        return handled
