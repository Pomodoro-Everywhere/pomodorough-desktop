"""Push button that wraps its label when its width budget is short.

UX round 2 batch C M3: at large fonts the one-line labels of the Timer
buttons are wider than the page. The Timer screen measures the width it
can give each button and hands it over as a width budget. While the label
fits the budget on one line the button sizes and paints exactly like a
plain QPushButton. Otherwise the label breaks between words, and the
button asks for the height of the wrapped lines.

The accessible text stays the full one-line label; only painting and size
hints see the line breaks.

UX round 3 DSK-23: a style sheet min-height or min-width becomes an
explicit minimum size when the style polishes the button, and a layout
keeps an explicit minimum over the size hint. The wrapped labels then got
the one-line height and were cut at the top and bottom. The size hint
already includes the style sheet floor (the style adds it in
sizeFromContents), so the button drops the explicit minimum after each
polish and lets the hint decide.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter

from .word_wrap import wrap_words


class WrappingPushButton(QPushButton):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._width_budget: int | None = None

    def set_width_budget(self, budget: int | None) -> None:
        """Widest the button may ask to be; None keeps the one-line label."""
        if budget == self._width_budget:
            return
        self._width_budget = budget
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        one_line = super().sizeHint()
        if self._width_budget is None or one_line.width() <= self._width_budget:
            return one_line
        lines = self._wrapped_lines(self._width_budget - self._label_chrome())
        return self._hint_for("\n".join(lines))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hint_for_label(self, text: str, budget: int | None) -> QSize:
        """Size the button would ask for with this label in this width budget.

        DSK-12: measures a label or a budget the button does not have now,
        and leaves the button as it is. The size hint the button caches
        follows the border it had when it was measured, at rest or with
        the focus ring, so this is the larger of the two.
        """
        chrome = self._label_chrome()
        if budget is not None and self._text_width(text) + chrome > budget:
            text = "\n".join(wrap_words(text, budget - chrome, self._text_width))
        return self._hint_for(text).expandedTo(self._hint_for(text, focused=True))

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        # The style polishes inside QWidget.event for Polish, and before the
        # StyleChange event when a style sheet changes.
        if event.type() in (QEvent.Type.Polish, QEvent.Type.StyleChange):
            self.follow_size_hint()
        return handled

    def follow_size_hint(self) -> None:
        """Drop the explicit minimum a style sheet polish set.

        Call it after polishing the button directly through its style,
        which sends neither a Polish nor a StyleChange event.
        """
        if not self.minimumSize().isNull():
            self.setMinimumSize(0, 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.width() >= super().sizeHint().width():
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = "\n".join(self._wrapped_lines(self.width() - self._label_chrome()))
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_PushButton, option)

    def _text_width(self, text: str) -> int:
        return self.fontMetrics().size(Qt.TextFlag.TextShowMnemonic, text).width()

    def _label_chrome(self) -> int:
        # Bevel, padding and border around the label, as the style measures them.
        return super().sizeHint().width() - self._text_width(self.text())

    def _hint_for(self, text: str, *, focused: bool = False) -> QSize:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        # Style sheet rules for focus, hover and press may change the border.
        # QPushButton caches its hint, so it never follows those states; the
        # wrapped hint measures the resting button for the same reason.
        transient = (
            QStyle.StateFlag.State_HasFocus
            | QStyle.StateFlag.State_MouseOver
            | QStyle.StateFlag.State_Sunken
        )
        option.state &= ~transient
        if focused:
            option.state |= QStyle.StateFlag.State_HasFocus
        content = self.fontMetrics().size(Qt.TextFlag.TextShowMnemonic, text)
        option.rect.setSize(content)
        return self.style().sizeFromContents(
            QStyle.ContentsType.CT_PushButton, option, content, self
        )

    def _wrapped_lines(self, room: int) -> list[str]:
        return wrap_words(self.text(), room, self._text_width)
