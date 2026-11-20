"""Combo box that ends a long choice with an ellipsis.

A choice can be wider than the page gives the combo, at a large font or in
a long translation. When the page narrows the combo below its choice, the
closed combo shows the start of the choice and ends it with an ellipsis, and
the popup elides the same way. How far the combo may narrow is up to its
owner.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QStylePainter


class ElidingComboBox(QComboBox):
    def __init__(self) -> None:
        super().__init__()
        self.view().setTextElideMode(Qt.TextElideMode.ElideRight)

    def text_field(self, option: QStyleOptionComboBox) -> QRect:
        """Where the style draws the current choice."""
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
        field = self.text_field(option)
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, field.width()
        )
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
