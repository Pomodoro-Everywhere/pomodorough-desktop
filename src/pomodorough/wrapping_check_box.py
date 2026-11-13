"""Check box that wraps its label between words when its width is short.

R54-D17: the one-line label of the Timer auto-start check box set the
narrowest width of the whole settings column, so at large fonts the
column, the clock and the controls asked for more than an 800-pixel screen.
This check box asks only for its widest word and reports the height its
wrapped label needs at the width it gets, like a word-wrapped QLabel.
While the label fits on one line it sizes and paints exactly like a plain
QCheckBox.

The accessible text stays the full one-line label; only painting, hit
testing and size hints see the line breaks.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
)

from .word_wrap import wrap_words


class WrappingCheckBox(QCheckBox):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        # QCheckBox never shrinks below its one-line hint and keeps that
        # height. A word-wrapped QLabel's policy lets the layout narrow the
        # box to its minimum hint and give the wrapped lines their height.
        policy = QSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
            QSizePolicy.ControlType.CheckBox,
        )
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def minimumSizeHint(self) -> QSize:
        one_line = super().sizeHint()
        words = self.text().split() or [self.text()]
        widest = max(self._text_size(word).width() for word in words)
        return QSize(min(one_line.width(), widest + self._label_chrome()), one_line.height())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._hint_for(self._label_at(width)).height()

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.width() >= super().sizeHint().width():
            super().paintEvent(event)
            return
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_CheckBox, self._wrapped_option())

    def hitButton(self, pos: QPoint) -> bool:
        # The click area follows the painted label, so every wrapped line
        # toggles the box as the one-line label does.
        area = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxClickRect, self._wrapped_option(), self
        )
        return area.contains(pos)

    def _wrapped_option(self) -> QStyleOptionButton:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self._label_at(self.width())
        return option

    def _label_at(self, width: int) -> str:
        if width >= super().sizeHint().width():
            return self.text()
        room = width - self._label_chrome()
        return "\n".join(wrap_words(self.text(), room, lambda text: self._text_size(text).width()))

    def _text_size(self, text: str) -> QSize:
        # Measured the way QCheckBox measures its own label.
        return self.style().itemTextRect(
            self.fontMetrics(), QRect(), Qt.TextFlag.TextShowMnemonic, False, text
        ).size()

    def _label_chrome(self) -> int:
        # Indicator, spacing and margins around the label, as the style measures them.
        return super().sizeHint().width() - self._text_size(self.text()).width()

    def _hint_for(self, text: str) -> QSize:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        return self.style().sizeFromContents(
            QStyle.ContentsType.CT_CheckBox, option, self._text_size(text), self
        )
