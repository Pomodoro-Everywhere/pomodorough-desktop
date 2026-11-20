"""Confirmation dialog for actions that discard local state."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def confirm_destructive_action(
    parent: QWidget | None, *, title: str, detail: str, action: str
) -> bool:
    """Ask before a destructive action. True only when the user confirmed.

    The confirm button carries the action's name instead of a generic Yes.
    Cancel is the default and the escape button, so Enter or Escape never
    discards state by accident.
    """
    dialog = QMessageBox(parent)
    try:
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(title)
        dialog.setText(title)
        dialog.setInformativeText(detail)
        confirm = dialog.addButton(action, QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return dialog.clickedButton() is confirm
    finally:
        dialog.deleteLater()
