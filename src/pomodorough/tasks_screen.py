from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
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
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self._render_task_row(
                row,
                task,
                summaries[task["id"]],
                mutations_enabled,
            )
        self.task_table.setVisible(bool(tasks))
        self.tasks_empty.setVisible(not tasks)

    @staticmethod
    def _signature(
        tasks: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mutations_enabled: bool,
    ) -> tuple[Any, ...]:
        return (
            datetime.now().astimezone().date(),
            mutations_enabled,
            tuple((task["id"], task["title"]) for task in tasks),
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

    def _render_totals(self, summaries: dict[str, dict[str, Any]]) -> None:
        total_finished = sum(summary["finished"] for summary in summaries.values())
        total_ms = sum(summary["timeMs"] for summary in summaries.values())
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
        self.task_table.setItem(row, 0, QTableWidgetItem(task["title"]))
        count = QTableWidgetItem(str(summary["finished"]))
        count.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_table.setItem(row, 1, count)
        spent = QTableWidgetItem(self._format_task_time(summary["timeMs"]))
        spent.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_table.setItem(row, 2, spent)
        delete = QPushButton(self.strings.text("task.delete"))
        delete.setObjectName("dangerButton")
        delete.setAccessibleName(
            self.strings.text("task.delete_accessible", task=task["title"])
        )
        delete.setEnabled(mutations_enabled)
        delete.clicked.connect(
            lambda checked=False, task_id=task["id"]: self.delete_task_requested.emit(
                task_id
            )
        )
        self.task_table.setCellWidget(row, 3, delete)

    def _format_task_time(self, milliseconds: int) -> str:
        minutes = max(0, milliseconds) // 60_000
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
        QLabel#emptyState { color: palette(mid); padding: 24px; }
        QTableWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); gridline-color: palette(alternate-base); outline: none; }
        QHeaderView::section { background: palette(button); color: palette(button-text); border: 1px solid palette(mid); padding: 6px; font-weight: 800; }
        """
