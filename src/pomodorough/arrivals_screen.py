from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .core import TERMINAL_STATUSES
from .localization import Strings


class ArrivalsScreen(QFrame):
    def __init__(self, strings: Strings, device_id: str) -> None:
        super().__init__()
        self.strings = strings
        self.setObjectName("ticket")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        self._build_body(layout, device_id)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.strings.text("arrivals.title"))
        title.setObjectName("sectionTitle")
        self.history_scope = QLabel(self.strings.text("arrivals.scope"))
        self.history_scope.setObjectName("taskSubtitle")
        self.history_scope.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(self.history_scope)
        self.history_count = QLabel("0")
        self.history_count.setObjectName("countBadge")
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(self.history_count)
        return header

    def _build_body(self, layout: QVBoxLayout, device_id: str) -> None:
        self.history_list = QListWidget()
        self.history_list.setAccessibleName(self.strings.text("arrivals.accessible"))
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.history_list, 1)
        self.device_label = QLabel(
            self.strings.text("device.label", device=device_id[-8:].upper())
        )
        self.device_label.setObjectName("device")
        layout.addWidget(self.device_label)

    def render(
        self,
        history: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
    ) -> None:
        retained = [item for item in history if item.get("status") in TERMINAL_STATUSES]
        self._render_header(retained)
        self.history_list.clear()
        for item in retained[:8]:
            self._render_item(item, known_tasks)
        if not retained:
            self._render_empty()

    def _render_header(self, retained: list[dict[str, Any]]) -> None:
        displayed = min(8, len(retained))
        self.history_count.setText(
            self.strings.text(
                "arrivals.count", displayed=displayed, total=len(retained)
            )
        )
        self.history_list.setAccessibleDescription(
            self.strings.text(
                "arrivals.description", displayed=displayed, total=len(retained)
            )
        )

    def _render_item(
        self,
        item: dict[str, Any],
        known_tasks: dict[str, dict[str, Any]],
    ) -> None:
        phase_value = str(item.get("phase", "timer"))
        phase = self._phase_label(phase_value)
        task_label = self._task_label(item.get("taskId"), known_tasks)
        status_label = self.strings.text(f"arrivals.status.{item.get('status')}")
        minutes = int(item.get("plannedDurationMs", 0)) // 60_000
        when = item.get("completedAt") or item.get("endedAt")
        text = self.strings.text(
            "arrivals.row",
            phase=phase,
            status=status_label,
            task=task_label,
            minutes=minutes,
            when=self._time_label(when),
            pending=self.strings.text("arrivals.pending")
            if item.get("pending")
            else "",
        )
        row = QListWidgetItem(text, self.history_list)
        row.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))

    def _phase_label(self, phase: str) -> str:
        key = f"phase.{phase}"
        return self.strings.text(key) if key in self.strings.messages else phase

    def _task_label(
        self,
        task_id: str | None,
        known_tasks: dict[str, dict[str, Any]],
    ) -> str:
        task = known_tasks.get(task_id) if task_id else None
        if task:
            return task["title"]
        if task_id:
            return self.strings.text("task.deleted")
        return self.strings.text("task.unassigned")

    def _time_label(self, when: object) -> str:
        try:
            return (
                datetime.fromisoformat(
                    str(when).replace("Z", "+00:00")  # noqa: FURB162
                )
                .astimezone()
                .strftime("%a %H:%M")
            )
        except (ValueError, TypeError):
            return self.strings.text("arrivals.time_pending")

    def _render_empty(self) -> None:
        text = self.strings.text("arrivals.empty")
        item = QListWidgetItem(text, self.history_list)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def stylesheet() -> str:
        return """
        QLabel#countBadge { background: palette(highlight); color: palette(highlighted-text); border: 2px solid palette(mid); padding: 2px 8px; font-weight: bold; }
        QListWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); outline: none; padding: 3px; }
        QListWidget::item { border-bottom: 1px solid palette(alternate-base); padding: 7px 5px; }
        """
