from __future__ import annotations

from PySide6.QtCore import QEvent, Signal
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent
from PySide6.QtWidgets import QApplication, QBoxLayout, QFrame, QLabel, QPushButton

from .localization import Strings


class TaskUndoBanner(QFrame):
    """Offers Undo and Dismiss for one deferred task delete.

    The banner has no timeout. Its owner decides when the undo window
    ends; the banner only reports which action the user chose.
    """

    undo_requested = Signal()
    dismiss_requested = Signal()

    def __init__(self, strings: Strings) -> None:
        super().__init__()
        self.strings = strings
        self.setObjectName("undoBanner")
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self.fontMetrics().averageCharWidth())
        self.label = QLabel()
        self.label.setObjectName("undoLabel")
        self.label.setWordWrap(True)
        self.undo_button = QPushButton(strings.text("task.undo"))
        self.undo_button.setObjectName("undoButton")
        self.undo_button.clicked.connect(self.undo_requested)
        self.dismiss_button = QPushButton(strings.text("task.undo_delete_now"))
        self.dismiss_button.setObjectName("undoDismissButton")
        self.dismiss_button.clicked.connect(self.dismiss_requested)
        self._layout.addWidget(self.label, 1)
        self._layout.addWidget(self.undo_button)
        self._layout.addWidget(self.dismiss_button)
        self.setVisible(False)

    def present(self, title: str) -> None:
        """Show the banner for the deleted task and tell screen readers."""
        banner_text = self.strings.text("task.deleted_banner", title=title)
        self.label.setText(banner_text)
        self.label.setAccessibleName(banner_text)
        self.undo_button.setAccessibleName(
            self.strings.text("task.undo_accessible", task=title)
        )
        self.dismiss_button.setAccessibleName(
            self.strings.text("task.undo_delete_now_accessible", task=title)
        )
        self.setVisible(True)
        self._announce(self.strings.text("task.undo_announcement", title=title))

    def _announce(self, message: str) -> None:
        # Focus stays on the task rows, so a screen reader would not read
        # the banner on its own. Qt drops the event when no assistive
        # technology is active.
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self, message))

    def fit_width(self, available_width: int) -> None:
        """Stack the label and actions when one line needs more width."""
        one_line = self._layout.spacing() * 2 + sum(
            widget.minimumSizeHint().width()
            for widget in (self.label, self.undo_button, self.dismiss_button)
        )
        direction = (
            QBoxLayout.Direction.LeftToRight
            if one_line <= available_width
            else QBoxLayout.Direction.TopToBottom
        )
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)

    def holds_focus(self) -> bool:
        focused = QApplication.focusWidget()
        return focused is not None and self.isAncestorOf(focused)

    def height_for(self, width: int) -> int:
        if self._layout.hasHeightForWidth():
            return self._layout.totalHeightForWidth(width)
        return self.sizeHint().height()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.FontChange:
            self._layout.setSpacing(self.fontMetrics().averageCharWidth())
        super().changeEvent(event)
