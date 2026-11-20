"""Typed confirmation before the Cloud account is deleted."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .localization import Strings

# The server accepts deletion only with this exact word. It is part of the
# wire request, so it is not localized.
CONFIRMATION_WORD = "DELETE"


class DeleteAccountDialog(QDialog):
    """Ask the user to type DELETE before the account is deleted.

    The delete button stays disabled until the field matches exactly. Enter
    with any other text keeps the dialog open and explains the mismatch next
    to the field instead of closing with a short status message.

    The dialog grows until its wrapped text is whole, within the screen. A
    window's layout enforces only its minimum size, and wrapped labels have
    a small one, so the notice would otherwise take its room from the
    prompt and cut the warning in the middle of a sentence.
    """

    def __init__(self, parent: QWidget | None, strings: Strings) -> None:
        super().__init__(parent)
        self.strings = strings
        self.setWindowTitle(strings.text("account.delete_prompt_title"))
        self._mismatch_text = strings.text("account.delete_mismatch_inline")
        self.prompt = QLabel(strings.text("account.delete_prompt"))
        self.prompt.setWordWrap(True)
        self.confirmation_input = QLineEdit()
        self.confirmation_input.setAccessibleName(
            strings.text("account.delete_input_accessible")
        )
        self.prompt.setBuddy(self.confirmation_input)
        self.mismatch_notice = QLabel(self._mismatch_text)
        self.mismatch_notice.setWordWrap(True)
        self.mismatch_notice.hide()
        self.buttons = QDialogButtonBox()
        self.delete_button = self._build_buttons(strings)
        layout = QVBoxLayout(self)
        layout.addWidget(self.prompt)
        layout.addWidget(self.confirmation_input)
        layout.addWidget(self.mismatch_notice)
        layout.addWidget(self.buttons)
        self.confirmation_input.textChanged.connect(self._confirmation_changed)
        self.confirmation_input.returnPressed.connect(self._submit)
        self.confirmation_input.setFocus()

    def _build_buttons(self, strings: Strings) -> QPushButton:
        delete_button = QPushButton(strings.text("account.delete_confirm"))
        delete_button.setObjectName("dangerButton")
        delete_button.setEnabled(False)
        self.buttons.addButton(
            delete_button, QDialogButtonBox.ButtonRole.DestructiveRole
        )
        cancel = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        # Enter belongs to the field: it submits the typed word or explains
        # the mismatch. No button may take it as the dialog default.
        for button in (delete_button, cancel):
            button.setAutoDefault(False)
            button.setDefault(False)
        delete_button.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        return delete_button

    def confirmation(self) -> str:
        return self.confirmation_input.text()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Qt opens a window at no more than two thirds of the screen width
        # and keeps the height it measured for the wider size hint.
        self._grow_to_show_all_text()

    def _grow_to_show_all_text(self) -> None:
        layout = self.layout()
        layout.activate()
        largest = self._largest_size_on_screen()
        size = self.size().expandedTo(self.sizeHint()).boundedTo(largest)
        text_height = layout.totalHeightForWidth(size.width())
        size.setHeight(min(max(size.height(), text_height), largest.height()))
        if size != self.size():
            self.resize(size)
            self._move_onto_screen()

    def _screen_area(self) -> QRect:
        return self.screen().availableGeometry()

    def _largest_size_on_screen(self) -> QSize:
        frame = self.frameGeometry().size() - self.geometry().size()
        return self._screen_area().size() - frame

    def _move_onto_screen(self) -> None:
        """A dialog that grew near a screen edge moves back inside it."""
        screen = self._screen_area()
        frame = self.frameGeometry()
        left = max(screen.left(), min(frame.left(), screen.right() - frame.width() + 1))
        top = max(screen.top(), min(frame.top(), screen.bottom() - frame.height() + 1))
        if QPoint(left, top) != frame.topLeft():
            self.move(left, top)

    def _confirmation_changed(self, text: str) -> None:
        self.delete_button.setEnabled(text == CONFIRMATION_WORD)
        self.mismatch_notice.hide()
        self.confirmation_input.setAccessibleDescription("")

    def _submit(self) -> None:
        if self.confirmation() == CONFIRMATION_WORD:
            self.accept()
            return
        self._show_mismatch_notice()
        self.confirmation_input.setAccessibleDescription(self._mismatch_text)

    def _show_mismatch_notice(self) -> None:
        notice = self.mismatch_notice
        # Once shown, the notice keeps its room while hidden, so the dialog
        # grows once and the prompt does not move on every keystroke.
        policy = notice.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        notice.setSizePolicy(policy)
        notice.show()
        self._grow_to_show_all_text()


def ask_delete_account_confirmation(
    parent: QWidget | None, strings: Strings
) -> str | None:
    """The confirmation word once the user confirmed, otherwise None."""
    dialog = DeleteAccountDialog(parent, strings)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.confirmation()
    finally:
        dialog.deleteLater()
