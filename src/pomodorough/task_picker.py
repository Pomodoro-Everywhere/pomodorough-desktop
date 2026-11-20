"""Focus task combo that keeps the start of a long title readable.

DSK-06: a task title can be much wider than the Timer screen gives the
picker. The closed picker shows the start of the title and ends it with an
ellipsis, the popup elides the same way, and the tooltip names the whole
title. The retarget hint stays in the accessible description and follows the
title in the tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QStylePainter

# Widths in characters of the font, so they grow with the font size. The
# picker asks for room for the preferred count and no more, so a long title
# never widens the Timer screen; a narrow window may squeeze it to the least
# count, which still shows the start of a title before the ellipsis.
_LEAST_CHARACTERS = 4
_PREFERRED_CHARACTERS = 20


class TaskPicker(QComboBox):
    def __init__(self) -> None:
        super().__init__()
        self._hint = ""
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.setMinimumContentsLength(_LEAST_CHARACTERS)
        self.view().setTextElideMode(Qt.TextElideMode.ElideRight)
        self.currentIndexChanged.connect(self._refresh_tool_tip)

    def set_hint(self, hint: str) -> None:
        self._hint = hint
        self.setAccessibleDescription(hint)
        self._refresh_tool_tip()

    def add_choice(self, title: str, task_id: str | None) -> None:
        self.addItem(title, task_id)
        if task_id is not None:
            self.setItemData(self.count() - 1, title, Qt.ItemDataRole.ToolTipRole)

    def _refresh_tool_tip(self) -> None:
        if self.currentData() is None:
            self.setToolTip(self._hint)
            return
        title = self.currentText()
        self.setToolTip(f"{title}\n{self._hint}" if self._hint else title)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        # QComboBox measures its minimum contents length in "X" advances.
        extra = self.fontMetrics().horizontalAdvance("X") * (
            _PREFERRED_CHARACTERS - _LEAST_CHARACTERS
        )
        return QSize(hint.width() + extra, hint.height())

    def shows_whole(self, title: str, width: int) -> bool:
        """Whether a picker this wide shows the title without an ellipsis."""
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.rect = QRect(0, 0, width, self.sizeHint().height())
        room = self._text_field(option).width()
        return self.fontMetrics().horizontalAdvance(title) <= room

    def _text_field(self, option: QStyleOptionComboBox) -> QRect:
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        field = self._text_field(option)
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, field.width()
        )
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
