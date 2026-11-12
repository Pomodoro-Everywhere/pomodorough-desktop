from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from .controller_outcomes import TaskChange
from .core import BlankTaskTitleError, TaskTitleTooLongError
from .localization import Strings
from .shared_core import SharedCoreError, SharedCoreOperationError

# Storage and Core host failures can pass on a second attempt. A value that
# the title rules or the Core reject fails the same way again, so it gets no
# retry action.
_RETRYABLE_FAILURES = (OSError, sqlite3.Error, SharedCoreError)

_RETRYABLE_SENTENCE_KEYS = {
    TaskChange.ADD: "task.error.add_failed",
    TaskChange.DELETE: "task.error.delete_failed",
    TaskChange.RENAME: "task.error.rename_failed",
}


@dataclass(frozen=True, slots=True)
class TaskFailureNotice:
    sentence: str
    can_retry: bool


def describe_task_failure(
    strings: Strings, change: TaskChange, error: Exception
) -> TaskFailureNotice:
    """Word a failed task change from the error type, never from its text."""
    if isinstance(error, BlankTaskTitleError):
        return TaskFailureNotice(strings.text("task.error.blank_title"), False)
    if isinstance(error, TaskTitleTooLongError):
        return TaskFailureNotice(strings.text("task.error.title_too_long"), False)
    # L10: a SharedCoreOperationError is a rejection, as in failure_notice.
    # The Core gives the same answer again, so Retry is not offered. The
    # controller still reports it: a rejection of a change the client had
    # already validated is a defect signal, not a user mistake.
    rejected = isinstance(error, SharedCoreOperationError)
    if isinstance(error, _RETRYABLE_FAILURES) and not rejected:
        return TaskFailureNotice(strings.text(_RETRYABLE_SENTENCE_KEYS[change]), True)
    return TaskFailureNotice(strings.text(_rejected_sentence_key(change)), False)


def _rejected_sentence_key(change: TaskChange) -> str:
    # A rejected delete has no task name to check.
    if change is TaskChange.DELETE:
        return "task.error.delete_rejected"
    return "task.error.change_rejected"


def build_retry_dialog(
    parent: QWidget, strings: Strings, sentence: str
) -> tuple[QMessageBox, QPushButton]:
    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("Pomodorough")
    dialog.setText(sentence)
    retry_button = dialog.addButton(
        strings.text("task.error.retry"), QMessageBox.ButtonRole.AcceptRole
    )
    cancel_button = dialog.addButton(
        strings.text("task.error.cancel"), QMessageBox.ButtonRole.RejectRole
    )
    dialog.setDefaultButton(retry_button)
    dialog.setEscapeButton(cancel_button)
    return dialog, retry_button


def ask_to_retry(parent: QWidget, strings: Strings, sentence: str) -> bool:
    """Show the failure with Retry and Cancel; report whether Retry was chosen."""
    dialog, retry_button = build_retry_dialog(parent, strings, sentence)
    dialog.exec()
    return dialog.clickedButton() is retry_button
