from __future__ import annotations

from typing import Protocol, cast

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QStackedWidget, QWidget

from .tasks_page import TasksPage


class PendingTaskDeleteHost(Protocol):
    """The window operations the undo window's lifetime depends on."""

    def complete_pending_task_delete_unattended(self) -> None: ...

    def report_unfinished_task_delete(self) -> None: ...


class TaskUndoLifetime(QObject):
    """Ends the task undo window when the user moves on from the Tasks page.

    The undo banner has no timeout. A deferred delete completes when another
    page is shown and when the window is hidden or minimized. Shutdown
    flushes on its own path. Switching to another application keeps the undo
    window open; the durable pending-delete marker covers a process that
    dies meanwhile.

    None of these triggers can carry a Retry dialog, so a completion that
    fails is held back and reported once the window is in view again.
    """

    _unfinished_delete_report_due = Signal()

    def __init__(
        self, window: QWidget, page_stack: QStackedWidget, tasks_page: TasksPage
    ) -> None:
        super().__init__(window)
        self._window = window
        self._host = cast(PendingTaskDeleteHost, window)
        self._page_stack = page_stack
        self._tasks_page = tasks_page
        page_stack.currentChanged.connect(self._page_changed)
        # Queued: the report opens a dialog, which must not run inside the
        # page change or the window event that asked for it.
        self._unfinished_delete_report_due.connect(
            self._host.report_unfinished_task_delete,
            Qt.ConnectionType.QueuedConnection,
        )
        window.installEventFilter(self)

    def _page_changed(self, _index: int) -> None:
        if self._page_stack.currentWidget() is not self._tasks_page:
            self._host.complete_pending_task_delete_unattended()
            self._unfinished_delete_report_due.emit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._window:
            return False
        if self._window_left_view(event):
            self._host.complete_pending_task_delete_unattended()
        elif self._window_came_into_view(event):
            self._unfinished_delete_report_due.emit()
        return False

    def _window_left_view(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Hide:
            return True
        return (
            event.type() == QEvent.Type.WindowStateChange
            and self._window.isMinimized()
        )

    def _window_came_into_view(self, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.WindowActivate):
            return True
        return (
            event.type() == QEvent.Type.WindowStateChange
            and not self._window.isMinimized()
        )
