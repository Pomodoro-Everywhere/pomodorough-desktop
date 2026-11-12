from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import normalize_task_title, shown_run_minutes, task_summaries_today
from .localization import Strings
from .railway_palette import PLATFORM, PLATFORM_DEEP, PORCELAIN
from .task_undo_banner import TaskUndoBanner

_DELETE_KEYS = (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
TITLE_COLUMN = 0


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    task_id: str
    title: str
    # Where the row stood, so Dismiss can focus the row that took its place.
    row: int


class TasksScreen(QFrame):
    add_task_requested = Signal(str)
    delete_task_requested = Signal(str)
    rename_task_requested = Signal(str, str)
    # A delete is about to wait in the undo window. The owner of task
    # changes records it durably inside this emit, before the row hides.
    pending_delete_started = Signal(str)
    # The user pressed Undo; the owner forgets the recorded delete.
    pending_delete_undone = Signal()
    # The undo window opened or ended; a host re-places the banner.
    undo_window_changed = Signal()

    def __init__(self, strings: Strings) -> None:
        super().__init__()
        self.strings = strings
        self._render_signature: tuple[Any, ...] | None = None
        self._last_render: tuple[list[dict[str, Any]], list[dict[str, Any]], bool] | None = None
        self._pending_deletion: PendingDeletion | None = None
        self._rename_target: tuple[str, str] | None = None
        self._in_layout_refresh = False
        self._row_resize_timer = QTimer(self)
        self._row_resize_timer.setSingleShot(True)
        self._row_resize_timer.timeout.connect(self._resize_task_rows)
        self._scheduled_scroll: tuple[int, int, int, int] | None = None
        self.setObjectName("ticket")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_form())
        self._build_table(layout)
        # Below the rows, so the banner never moves a row under the pointer.
        layout.addWidget(self._build_undo_banner())

    def _build_header(self) -> QVBoxLayout:
        header = QVBoxLayout()
        self.task_title = QLabel(self.strings.text("task.board"))
        self.task_title.setObjectName("sectionTitle")
        self.task_title.setWordWrap(True)
        header.addWidget(self.task_title)
        self.task_subtitle = QLabel(self.strings.text("task.board_detail"))
        self.task_subtitle.setObjectName("taskSubtitle")
        self.task_subtitle.setWordWrap(True)
        header.addWidget(self.task_subtitle)
        self.task_totals = QLabel(self._empty_totals())
        self.task_totals.setObjectName("countBadge")
        self.task_totals.setWordWrap(True)
        header.addWidget(self.task_totals)
        return header

    def _empty_totals(self) -> str:
        return self.strings.text(
            "task.totals",
            count=0,
            unit=self.strings.text("task.pomodoro.other"),
            minutes=self.strings.text("duration.minutes", minutes=0).upper(),
        )

    def _build_form(self) -> QHBoxLayout:
        form = QHBoxLayout()
        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText(self.strings.text("task.placeholder"))
        self.task_input.setAccessibleName(self.strings.text("task.name_accessible"))
        self.task_input.returnPressed.connect(self._request_add_task)
        self.add_task_button = QPushButton(self.strings.text("task.add"))
        self.add_task_button.setObjectName("primaryButton")
        self.add_task_button.clicked.connect(self._request_add_task)
        form.addWidget(self.task_input, 1)
        form.addWidget(self.add_task_button)
        return form

    def _request_add_task(self) -> None:
        self.flush_pending_delete()
        self.add_task_requested.emit(self.task_input.text())

    def _build_undo_banner(self) -> TaskUndoBanner:
        banner = TaskUndoBanner(self.strings)
        banner.undo_requested.connect(self.undo_pending_delete)
        banner.dismiss_requested.connect(self.dismiss_pending_delete)
        self.undo_banner = banner
        self.undo_label = banner.label
        self.undo_button = banner.undo_button
        self.undo_dismiss_button = banner.dismiss_button
        return banner

    def rest_undo_banner(self, host_width: int) -> None:
        """Keep the banner under the table when a host does not dock it."""
        banner = self.undo_banner
        inset = self.width() - self.layout().contentsRect().width()
        banner.fit_width(host_width - inset)
        if banner.parentWidget() is self:
            return
        held_focus = banner.holds_focus()
        self.layout().addWidget(banner)
        banner.setVisible(self._pending_deletion is not None)
        if held_focus and banner.isVisible():
            banner.undo_button.setFocus()

    def link_undo_tab_order(self) -> None:
        """Tab reaches Undo, then Dismiss, right after the task rows."""
        banner = self.undo_banner
        last_row = self.task_table.rowCount() - 1
        before = self.task_table.cellWidget(last_row, 3) if last_row >= 0 else None
        if before is None:
            before = self.add_task_button
        if before.window() is not banner.window():
            return
        QWidget.setTabOrder(before, banner.undo_button)
        QWidget.setTabOrder(banner.undo_button, banner.dismiss_button)

    def _build_table(self, layout: QVBoxLayout) -> None:
        self.task_table = QTableWidget(0, 4)
        self.task_table.setAccessibleName(self.strings.text("task.board_accessible"))
        self.task_table.setHorizontalHeaderLabels(
            tuple(
                self.strings.text(f"task.column.{key}")
                for key in ("task", "finished", "time", "action")
            )
        )
        self._configure_table_headers()
        self.task_table.verticalHeader().hide()
        self.task_table.installEventFilter(self)
        self.task_table.viewport().installEventFilter(self)
        self.task_table.horizontalHeader().sectionResized.connect(
            self._schedule_row_resize
        )
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.task_table.setTabKeyNavigation(False)
        self.task_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self._show_task_context_menu)
        self.task_table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.task_table.itemDelegate().closeEditor.connect(self._rename_editor_closed)
        self.task_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.task_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.task_table, 1)
        self.tasks_empty = QLabel(self.strings.text("task.empty"))
        self.tasks_empty.setObjectName("emptyState")
        self.tasks_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # B1: the hint wraps, so its longest line never widens the page past
        # the viewport and pushes the Add button out of view.
        self.tasks_empty.setWordWrap(True)
        layout.addWidget(self.tasks_empty, 1)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and self._handle_table_key(
            watched, event
        ):
            return True
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.LayoutRequest,
            QEvent.Type.FocusIn,
            QEvent.Type.Resize,
        ):
            self._schedule_row_resize()
        return super().eventFilter(watched, event)

    def _schedule_row_resize(self) -> None:
        # Wait for inherited styles and cell widgets to finish updating their hints.
        if self._in_layout_refresh:
            return
        if not self._row_resize_timer.isActive():
            table = self.task_table
            bar_v = table.verticalScrollBar()
            bar_h = table.horizontalScrollBar()
            self._scheduled_scroll = (
                bar_v.value(),
                bar_h.value(),
                bar_v.maximum(),
                bar_h.maximum(),
            )
        self._row_resize_timer.start()

    @staticmethod
    def _restore_task_scroll(
        table: QTableWidget,
        scheduled: tuple[int, int, int, int] | None,
    ) -> tuple[int, int] | None:
        bar_v = table.verticalScrollBar()
        bar_h = table.horizontalScrollBar()
        current = (bar_v.value(), bar_h.value())
        if scheduled is None:
            fresh_v = True
            fresh_h = True
        elif (scheduled[2], scheduled[3]) != (bar_v.maximum(), bar_h.maximum()):
            # Range collapse clamps values without any real scroll; fall
            # back to restoring instead of stranding the content.
            fresh_v = True
            fresh_h = True
        else:
            fresh_v = scheduled[0] == current[0]
            fresh_h = scheduled[1] == current[1]
        anchor = table.rowAt(0)
        anchor_offset = table.rowViewportPosition(anchor)
        header = table.horizontalHeader()
        was_blocked = header.blockSignals(True)
        try:
            table.resizeRowsToContents()
            table.resizeColumnToContents(3)
            table.doItemsLayout()
        finally:
            header.blockSignals(was_blocked)
        if fresh_h:
            table.horizontalScrollBar().setValue(current[1])
        if not fresh_v:
            # A newer scroll superseded this pass; leave it alone.
            return None
        table.verticalScrollBar().setValue(current[0])
        if 0 <= anchor < table.rowCount():
            # Rows above the viewport may have changed height, which moves
            # every row under the same per-pixel value. Keep the top row
            # where it was so the content does not jump.
            drift = table.rowViewportPosition(anchor) - anchor_offset
            if drift:
                bar_v.setValue(current[0] + drift)
        return (
            table.verticalScrollBar().value(),
            table.horizontalScrollBar().value(),
        )

    def _resize_task_rows(self) -> None:
        if self._in_layout_refresh:
            return
        self._in_layout_refresh = True
        scroll_at_post: tuple[int, int] | None = None
        try:
            table = self.task_table
            scheduled = self._scheduled_scroll
            self._scheduled_scroll = None
            scroll_at_post = self._restore_task_scroll(table, scheduled)
            self._reserve_task_row()
            self._reflow_header_text()
        finally:
            self._in_layout_refresh = False
        # A newer scroll owns the table position when stale, but the page
        # can still hide the button (header growth moves the table), so a
        # reveal always runs: it only recenters a settled table.
        QTimer.singleShot(
            0,
            self,
            partial(self._deliver_focus_reveal, table_scroll=scroll_at_post),
        )

    def _deliver_focus_reveal(self, table_scroll: tuple[int, int] | None) -> None:
        # The receiver cancels this callback once the screen is destroyed,
        # but PySide6 drops exceptions raised by receiver-bound callbacks
        # instead of calling sys.excepthook. Report them to the installed
        # hook (the Sentry integration), as the receiver-free form does.
        try:
            self._reveal_focused_delete(table_scroll)
        except Exception:
            sys.excepthook(*sys.exc_info())

    def _reveal_focused_delete(
        self, table_scroll: tuple[int, int] | None = None
    ) -> None:
        focused_id, row = self._focused_delete_id()
        if focused_id is None:
            return
        button = self.task_table.cellWidget(row, 3)
        if button is None:
            return
        table = self.task_table
        viewport = table.viewport()
        if (
            table_scroll is not None
            and table_scroll
            == (
                table.verticalScrollBar().value(),
                table.horizontalScrollBar().value(),
            )
            and not viewport.rect().contains(
                button.mapTo(viewport, button.rect().center())
            )
        ):
            table.scrollTo(
                table.model().index(row, 3),
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
        page = self.parentWidget()
        if page is not None and isinstance(page.parentWidget(), QScrollArea):
            page.parentWidget().ensureWidgetVisible(button, 0, 0)

    def tallest_row_height(self) -> int:
        table = self.task_table
        return max(
            (table.rowHeight(row) for row in range(table.rowCount())),
            default=table.verticalHeader().defaultSectionSize(),
        )

    def _reserve_task_row(self) -> None:
        # Keep one complete row reachable; the page scrolls instead of crushing it.
        table = self.task_table
        table.setMinimumHeight(
            table.horizontalHeader().height()
            + self.tallest_row_height()
            + table.horizontalScrollBar().sizeHint().height()
            + table.frameWidth() * 2
        )

    def _reflow_header_text(self) -> None:
        # Wrapped labels report a single-line minimum height, so the page
        # column would squeeze them vertically. Claim the wrapped height.
        # Clear first: heightForWidth honors the current minimum height,
        # which would freeze a stale enlarged value after font shrink.
        for label in (self.task_title, self.task_subtitle, self.task_totals):
            width = label.width()
            if width <= 0 or not label.isVisible():
                continue
            label.setMinimumHeight(0)
            label.setMinimumHeight(label.heightForWidth(width))

    def _configure_table_headers(self) -> None:
        alignments = (
            Qt.AlignmentFlag.AlignLeft,
            Qt.AlignmentFlag.AlignCenter,
            Qt.AlignmentFlag.AlignCenter,
            Qt.AlignmentFlag.AlignCenter,
        )
        for column, alignment in enumerate(alignments):
            self.task_table.horizontalHeaderItem(column).setTextAlignment(alignment)
        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    def render(
        self,
        tasks: list[dict[str, Any]],
        history: list[dict[str, Any]],
        *,
        mutations_enabled: bool,
    ) -> None:
        self._last_render = (tasks, history, mutations_enabled)
        tasks = self._without_pending_deletion(tasks)
        signature = self._signature(tasks, history, mutations_enabled)
        if signature == self._render_signature:
            return
        self._render_signature = signature
        # Rows may move under an open title editor; drop the edit instead
        # of committing it into another task's cell.
        self._cancel_rename()
        summaries = task_summaries_today(tasks, history)
        self._render_totals(summaries)
        self.task_input.setEnabled(mutations_enabled)
        self.add_task_button.setEnabled(mutations_enabled)
        focused_id, focused_row = self._focused_delete_id()
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self._render_task_row(
                row,
                task,
                self._summary_for(task, summaries),
                mutations_enabled,
            )
        self.task_table.setVisible(bool(tasks))
        self.tasks_empty.setVisible(not tasks)
        self._restore_delete_focus(focused_id, focused_row)
        self.link_undo_tab_order()
        self._schedule_row_resize()

    def _without_pending_deletion(
        self, tasks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        pending = self._pending_deletion
        if pending is None:
            return tasks
        return [
            task
            for task in tasks
            if not (isinstance(task, dict) and task.get("id") == pending.task_id)
        ]

    def _rerender(self) -> None:
        if self._last_render is None:
            return
        tasks, history, mutations_enabled = self._last_render
        self.render(tasks, history, mutations_enabled=mutations_enabled)

    @staticmethod
    def _signature(
        tasks: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mutations_enabled: bool,
    ) -> tuple[Any, ...]:
        return (
            datetime.now().astimezone().date(),
            mutations_enabled,
            tuple(
                (
                    task.get("id") if isinstance(task, dict) else None,
                    task.get("title") if isinstance(task, dict) else None,
                )
                for task in tasks
            ),
            tuple(
                (
                    item.get("id"),
                    item.get("taskId"),
                    item.get("phase"),
                    item.get("status"),
                    item.get("plannedDurationMs"),
                    item.get("elapsedMs"),
                    item.get("completedAt") or item.get("endedAt"),
                )
                for item in history
            ),
        )

    @staticmethod
    def _coerce_count(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _summary_for(
        task: dict[str, Any], summaries: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        # History-derived summaries may miss a task id (deleted/migrated
        # tasks, clock skew across days); render a zero placeholder
        # instead of raising KeyError mid-render.
        task_id = task.get("id") if isinstance(task, dict) else None
        summary = summaries.get(task_id)
        if summary is None and task_id is not None:
            summary = summaries.get(str(task_id))
        if isinstance(summary, dict):
            return summary
        return {"finished": 0, "timeMs": 0}

    def _render_totals(self, summaries: dict[str, dict[str, Any]]) -> None:
        total_finished = sum(
            self._coerce_count(summary.get("finished")) for summary in summaries.values()
        )
        total_ms = sum(
            self._coerce_count(summary.get("timeMs")) for summary in summaries.values()
        )
        self.task_totals.setText(
            self.strings.text(
                "task.totals",
                count=total_finished,
                unit=self.strings.plural("task.pomodoro", total_finished),
                minutes=self._format_task_time(total_ms).upper(),
            )
        )

    def _render_task_row(
        self,
        row: int,
        task: dict[str, Any],
        summary: dict[str, Any],
        mutations_enabled: bool,
    ) -> None:
        title = task.get("title") if isinstance(task, dict) else None
        title = title if isinstance(title, str) and title else ""
        task_id = task.get("id") if isinstance(task, dict) else None
        task_id = task_id if isinstance(task_id, str) else ""
        finished = summary.get("finished", 0) if isinstance(summary, dict) else 0
        time_ms = summary.get("timeMs", 0) if isinstance(summary, dict) else 0
        self._set_text_cell(row, 0, title, None)
        self._set_text_cell(
            row, 1, str(finished), Qt.AlignmentFlag.AlignCenter
        )
        self._set_text_cell(
            row, 2, self._format_task_time(time_ms), Qt.AlignmentFlag.AlignCenter
        )
        self._render_delete_cell(row, task_id, title, mutations_enabled)

    def _set_text_cell(
        self, row: int, column: int, text: str, alignment: Qt.AlignmentFlag | None
    ) -> None:
        existing = self.task_table.item(row, column)
        if existing is None:
            cell = QTableWidgetItem(text)
            if alignment is not None:
                cell.setTextAlignment(alignment)
            if column != TITLE_COLUMN:
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.task_table.setItem(row, column, cell)
            return
        if existing.text() != text:
            existing.setText(text)

    def _render_delete_cell(
        self, row: int, task_id: str, title: str, mutations_enabled: bool
    ) -> None:
        accessible = self.strings.text("task.delete_accessible", task=title)
        existing = self.task_table.cellWidget(row, 3)
        if existing is not None and existing.property("taskId") == task_id:
            existing.setAccessibleName(accessible)
            existing.setEnabled(mutations_enabled)
            return
        if existing is not None:
            existing.deleteLater()
        delete = QPushButton(self.strings.text("task.delete"))
        delete.setObjectName("dangerButton")
        delete.setProperty("taskId", task_id)
        delete.setAccessibleName(accessible)
        delete.setEnabled(mutations_enabled)
        delete.installEventFilter(self)
        delete.clicked.connect(
            lambda checked=False, task_id=task_id: self._request_delete(task_id)
        )
        self.task_table.setCellWidget(row, 3, delete)

    # Deletion is deferred so the row can be restored from the banner. The
    # undo window has no timeout: it ends on Undo, Dismiss, another delete,
    # an add or rename, or when the owner flushes it (leaving the Tasks
    # screen, a timer action, the window hiding, minimizing or closing).
    # Switching to another application keeps the window open; the owner's
    # durable record covers a process that dies meanwhile.

    def _request_delete(self, task_id: str) -> None:
        if not task_id:
            return
        self.flush_pending_delete()
        title = self._title_for(task_id)
        row = self._row_for_delete_id(task_id)
        self._pending_deletion = PendingDeletion(task_id, title, row)
        self.pending_delete_started.emit(task_id)
        if self._pending_deletion is None:
            # The owner could not record the delete and withdrew it.
            return
        self.undo_banner.present(title)
        self.undo_window_changed.emit()
        self._rerender()

    def _title_for(self, task_id: str) -> str:
        tasks = self._last_render[0] if self._last_render is not None else []
        for task in tasks:
            if isinstance(task, dict) and task.get("id") == task_id:
                title = task.get("title")
                return title if isinstance(title, str) else ""
        return ""

    def pending_delete_task_id(self) -> str | None:
        pending = self._pending_deletion
        return pending.task_id if pending is not None else None

    def _clear_pending_delete(self) -> PendingDeletion | None:
        pending = self._pending_deletion
        self._pending_deletion = None
        self.undo_banner.setVisible(False)
        if pending is not None:
            self.undo_window_changed.emit()
        return pending

    def flush_pending_delete(self) -> None:
        """Request the deferred delete now; a no-op when nothing is pending."""
        pending = self._clear_pending_delete()
        if pending is None:
            return
        self.delete_task_requested.emit(pending.task_id)
        # The owner normally re-renders without the task inside the emit;
        # otherwise the hidden row comes back.
        self._rerender()

    def dismiss_pending_delete(self) -> None:
        """Complete the deferred delete because the user dismissed Undo."""
        held_focus = self.undo_banner.holds_focus()
        pending = self._pending_deletion
        self.flush_pending_delete()
        if not held_focus or pending is None:
            return
        # The row that took the deleted row's place, else the last row.
        neighbour = min(max(pending.row, 0), self.task_table.rowCount() - 1)
        button = self.task_table.cellWidget(neighbour, 3)
        (button if button is not None else self.task_input).setFocus()

    def undo_pending_delete(self) -> None:
        pending = self._clear_pending_delete()
        if pending is None:
            return
        self.pending_delete_undone.emit()
        self._rerender()
        row = self._row_for_delete_id(pending.task_id)
        button = self.task_table.cellWidget(row, 3) if row >= 0 else None
        if button is not None:
            button.setFocus()

    # Keyboard: Delete/Backspace remove the targeted row, F2 renames it.

    def _handle_table_key(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(event, QKeyEvent) or self._rename_editor() is not None:
            return False
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            return False
        key = event.key()
        if key not in _DELETE_KEYS and key != Qt.Key.Key_F2:
            return False
        task_id, row = self._keyboard_target(watched)
        if task_id is None:
            return False
        if key == Qt.Key.Key_F2:
            self._begin_rename(row)
        else:
            self._request_delete(task_id)
        return True

    def _keyboard_target(self, watched: QObject) -> tuple[str | None, int]:
        if isinstance(watched, QPushButton):
            row = self._row_for_delete_id(watched.property("taskId"))
        else:
            row = self.task_table.currentRow()
        task_id = self._task_id_at(row)
        return (task_id, row) if task_id is not None else (None, -1)

    def _cell_double_clicked(self, row: int, column: int) -> None:
        if column == TITLE_COLUMN:
            self._begin_rename(row)

    def _show_task_context_menu(self, position: QPoint) -> None:
        row = self.task_table.indexAt(position).row()
        if row < 0:
            row = self.task_table.currentRow()
        menu = self._task_context_menu(row)
        if menu is None:
            return
        menu.exec(self.task_table.viewport().mapToGlobal(position))

    def _task_context_menu(self, row: int) -> QMenu | None:
        if self._task_id_at(row) is None:
            return None
        menu = QMenu(self.task_table)
        rename = menu.addAction(self.strings.text("task.rename"))
        rename.triggered.connect(
            lambda checked=False, row=row: self._begin_rename(row)
        )
        return menu

    def _begin_rename(self, row: int) -> None:
        task_id = self._task_id_at(row)
        item = self.task_table.item(row, TITLE_COLUMN) if row >= 0 else None
        if task_id is None or item is None or self._rename_editor() is not None:
            return
        self._rename_target = (task_id, item.text())
        self.task_table.setCurrentCell(row, TITLE_COLUMN)
        self.task_table.editItem(item)
        if self.task_table.cellWidget(row, TITLE_COLUMN) is None:
            self._rename_target = None

    def _task_id_at(self, row: int) -> str | None:
        button = self.task_table.cellWidget(row, 3) if row >= 0 else None
        if button is None or not button.isEnabled():
            return None
        task_id = button.property("taskId")
        return task_id if isinstance(task_id, str) and task_id else None

    def _rename_editor(self) -> QWidget | None:
        if self._rename_target is None:
            return None
        row = self._row_for_delete_id(self._rename_target[0])
        if row < 0:
            self._rename_target = None
            return None
        return self.task_table.cellWidget(row, TITLE_COLUMN)

    def _cancel_rename(self) -> None:
        editor = self._rename_editor()
        target = self._rename_target
        self._rename_target = None
        if editor is None or target is None:
            return
        row = self._row_for_delete_id(target[0])
        item = self.task_table.item(row, TITLE_COLUMN)
        if item is not None:
            self.task_table.closePersistentEditor(item)
            item.setText(target[1])

    def _rename_editor_closed(
        self, editor: QWidget, hint: QAbstractItemDelegate.EndEditHint
    ) -> None:
        target = self._rename_target
        if target is None:
            return
        self._rename_target = None
        task_id, original = target
        item = self.task_table.item(self._row_for_delete_id(task_id), TITLE_COLUMN)
        if item is None:
            return
        title = item.text()
        item.setText(original)
        if hint == QAbstractItemDelegate.EndEditHint.RevertModelCache:
            return
        # A blank title still goes to the controller, which refuses it with
        # the same sentence as the composer.
        if normalize_task_title(title) == normalize_task_title(original):
            return
        self.flush_pending_delete()
        self.rename_task_requested.emit(task_id, title)

    def _focused_delete_id(self) -> tuple[str | None, int]:
        focused = QApplication.focusWidget()
        for row in range(self.task_table.rowCount()):
            if self.task_table.cellWidget(row, 3) is focused and focused is not None:
                value = focused.property("taskId")
                return (value if isinstance(value, str) else None, row)
        return (None, -1)

    def _row_for_delete_id(self, task_id: str) -> int:
        for row in range(self.task_table.rowCount()):
            button = self.task_table.cellWidget(row, 3)
            if button is not None and button.property("taskId") == task_id:
                return row
        return -1

    def _restore_delete_focus(
        self, focused_id: str | None, focused_row: int
    ) -> None:
        if focused_id is None and focused_row < 0:
            return
        if focused_id is not None:
            row = self._row_for_delete_id(focused_id)
            if row >= 0:
                button = self.task_table.cellWidget(row, 3)
                if button is not None and QApplication.focusWidget() is not button:
                    button.setFocus()
                return
        if self.task_table.rowCount() == 0:
            self.task_input.setFocus()
            return
        neighbor = min(max(0, focused_row), self.task_table.rowCount() - 1)
        button = self.task_table.cellWidget(neighbor, 3)
        if button is not None and QApplication.focusWidget() is not button:
            button.setFocus()

    def _format_task_time(self, milliseconds: int) -> str:
        try:
            total_ms = max(0, int(milliseconds))
        except (TypeError, ValueError):
            total_ms = 0
        minutes = shown_run_minutes(total_ms)
        hours, remaining = divmod(minutes, 60)
        if hours and remaining:
            return self.strings.text(
                "duration.hours_minutes", hours=hours, minutes=remaining
            )
        if hours:
            return self.strings.text("duration.hours", hours=hours)
        return self.strings.text("duration.minutes", minutes=remaining)

    @staticmethod
    def stylesheet() -> str:
        return """
        QLabel#countBadge { background: @platform; color: @porcelain; border: 2px solid @platform_deep; padding: 2px 8px; font-weight: bold; }
        QLabel#emptyState { color: palette(text); padding: 24px; }
        QFrame#undoBanner { background: palette(base); color: palette(text); border: 2px solid palette(mid); padding: 2px 8px; }
        QTableWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); gridline-color: palette(alternate-base); outline: none; }
        QHeaderView::section { background: palette(button); color: palette(button-text); border: 1px solid palette(mid); padding: 6px; font-weight: 800; }
        """.replace("@platform_deep", PLATFORM_DEEP).replace("@platform", PLATFORM).replace("@porcelain", PORCELAIN)
