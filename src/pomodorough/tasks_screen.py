from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .core import task_summaries_today
from .localization import Strings


class TasksScreen(QFrame):
    add_task_requested = Signal(str)
    delete_task_requested = Signal(str)

    def __init__(self, strings: Strings) -> None:
        super().__init__()
        self.strings = strings
        self._render_signature: tuple[Any, ...] | None = None
        self.setObjectName("ticket")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_form())
        self._build_table(layout)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.strings.text("task.board"))
        title.setObjectName("sectionTitle")
        subtitle = QLabel(self.strings.text("task.board_detail"))
        subtitle.setObjectName("taskSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.task_totals = QLabel(self._empty_totals())
        self.task_totals.setObjectName("countBadge")
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
        self.add_task_requested.emit(self.task_input.text())

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
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.task_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.task_table, 1)
        self.tasks_empty = QLabel(self.strings.text("task.empty"))
        self.tasks_empty.setObjectName("emptyState")
        self.tasks_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.tasks_empty)

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
        signature = self._signature(tasks, history, mutations_enabled)
        if signature == self._render_signature:
            return
        self._render_signature = signature
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
            self.task_table.setItem(row, column, cell)
            return
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
        delete.clicked.connect(
            lambda checked=False, task_id=task_id: self.delete_task_requested.emit(
                task_id
            )
        )
        self.task_table.setCellWidget(row, 3, delete)

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
        minutes = total_ms // 60_000
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
        QLabel#countBadge { background: palette(highlight); color: palette(highlighted-text); border: 2px solid palette(mid); padding: 2px 8px; font-weight: bold; }
        QLabel#emptyState { color: palette(text); padding: 24px; }
        QTableWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); gridline-color: palette(alternate-base); outline: none; }
        QHeaderView::section { background: palette(button); color: palette(button-text); border: 1px solid palette(mid); padding: 6px; font-weight: 800; }
        """
