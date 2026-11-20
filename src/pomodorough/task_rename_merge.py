"""Confirmation before a rename merges one task into another.

DSK-16: a task's identity is its normalized title, so renaming a task onto
the title of another task joins the two. The Core rename then carries the
renamed task's finished history to the existing task. That join cannot be
undone from the board, so the user confirms it first.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from .localization import Strings


def build_merge_dialog(
    parent: QWidget, strings: Strings, renamed: str, existing: str
) -> tuple[QMessageBox, QPushButton]:
    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setWindowTitle(strings.text("task.merge.title"))
    dialog.setText(strings.text("task.merge.question", task=renamed, existing=existing))
    dialog.setInformativeText(strings.text("task.merge.detail", existing=existing))
    merge_button = dialog.addButton(
        strings.text("task.merge.confirm"), QMessageBox.ButtonRole.AcceptRole
    )
    cancel_button = dialog.addButton(
        strings.text("task.error.cancel"), QMessageBox.ButtonRole.RejectRole
    )
    # Escape and Return both keep the two tasks apart.
    dialog.setDefaultButton(cancel_button)
    dialog.setEscapeButton(cancel_button)
    return dialog, merge_button


def confirm_rename_merge(
    parent: QWidget, strings: Strings, renamed: str, existing: str
) -> bool:
    """Ask whether ``renamed`` may merge into ``existing``; True means merge."""
    dialog, merge_button = build_merge_dialog(parent, strings, renamed, existing)
    dialog.exec()
    return dialog.clickedButton() is merge_button
