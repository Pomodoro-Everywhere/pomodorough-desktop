from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .core import (
    ACTIVE_STATUSES,
    PHASES,
    TERMINAL_STATUSES,
    completed_focus_count_for_day,
    elapsed_ms,
    format_remaining,
    long_break_progress,
    timer_for_display,
)
from .localization import Strings
from .timer_view import ClockWidget


@dataclass(frozen=True)
class TimerRenderState:
    source_timer: dict[str, Any]
    display_timer: dict[str, Any]
    status: str
    elapsed: int
    planned: int
    remaining: int

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class TimerScreen(QWidget):
    primary_action_requested = Signal()
    command_requested = Signal(str)
    stop_sound_requested = Signal()
    phase_selected = Signal(str)
    duration_changed = Signal(str, int)
    task_selection_changed = Signal(int)
    auto_breaks_changed = Signal(bool)

    def __init__(self, strings: Strings, settings: dict[str, Any]) -> None:
        super().__init__()
        self.strings = strings
        self._task_selector_signature: tuple[Any, ...] | None = None
        self.content_layout = QHBoxLayout(self)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self._build_left_panel()
        self._build_settings_panel(settings)

    def _build_left_panel(self) -> None:
        self.left_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.clock = ClockWidget(self.strings)
        self.left_layout.addWidget(self.clock, 1)
        self.long_break_progress = QLabel("○○○○")
        self.long_break_progress.setObjectName("microLabel")
        self.long_break_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.long_break_progress.setAccessibleName(
            self.strings.text("status.pomodoro_progress")
        )
        self.left_layout.addWidget(self.long_break_progress)
        self.active_task_context = QLabel("")
        self.active_task_context.setObjectName("taskSubtitle")
        self.active_task_context.setAccessibleName(
            self.strings.text("task.active_accessible")
        )
        self.active_task_context.setWordWrap(True)
        self.left_layout.addWidget(self.active_task_context)
        self._build_actions()
        self.content_layout.addLayout(self.left_layout, 3)

    def _build_actions(self) -> None:
        self.actions_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.actions_layout.setSpacing(8)
        self._build_task_selector()
        self.primary_button = QPushButton(
            self.strings.text(
                "status.primary.start",
                phase=self.strings.text("phase.focus").upper(),
            )
        )
        self.primary_button.setObjectName("primaryButton")
        self.primary_button.clicked.connect(self.primary_action_requested)
        self.finish_button = QPushButton(self.strings.text("status.finish"))
        self.finish_button.clicked.connect(
            lambda: self.command_requested.emit("finish")
        )
        self.cancel_button = QPushButton(self.strings.text("status.cancel"))
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(
            lambda: self.command_requested.emit("cancel")
        )
        self.stop_sound_button = QPushButton(self.strings.text("status.stop_sound"))
        self.stop_sound_button.clicked.connect(self.stop_sound_requested)
        self.stop_sound_button.setVisible(False)
        for widget in self._action_widgets():
            self.actions_layout.addWidget(widget, 1)
        self.left_layout.addLayout(self.actions_layout)

    def _build_task_selector(self) -> None:
        self.task_selector_panel = QFrame()
        self.task_selector_panel.setObjectName("taskSelector")
        self.task_selector_panel.setMinimumWidth(190)
        self.task_selector_panel.setMaximumHeight(76)
        layout = QHBoxLayout(self.task_selector_panel)
        layout.setContentsMargins(8, 5, 8, 7)
        layout.setSpacing(8)
        label = QLabel(self.strings.text("task.focus").upper())
        label.setObjectName("microLabel")
        self.task_combo = QComboBox()
        self.task_combo.setAccessibleName(self.strings.text("task.focus"))
        self.task_combo.currentIndexChanged.connect(self.task_selection_changed)
        layout.addWidget(label)
        layout.addWidget(self.task_combo, 1)

    def _action_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.task_selector_panel,
            self.primary_button,
            self.finish_button,
            self.cancel_button,
            self.stop_sound_button,
        )

    def _build_settings_panel(self, settings: dict[str, Any]) -> None:
        self.right_panel = QFrame()
        self.right_panel.setObjectName("ticket")
        self.right_layout = QVBoxLayout(self.right_panel)
        self._build_pattern_heading()
        self._build_phase_buttons()
        self._build_duration_controls(settings)
        self._build_auto_break_controls(settings)
        self._build_alert_notice()
        self.right_layout.addStretch()
        self.content_layout.addWidget(self.right_panel, 2)
        self.right_panel.hide()

    def _build_pattern_heading(self) -> None:
        label = QLabel(self.strings.text("pattern.title"))
        label.setObjectName("sectionTitle")
        self.right_layout.addWidget(label)
        detail = QLabel(self.strings.text("pattern.detail"))
        detail.setObjectName("taskSubtitle")
        self.right_layout.addWidget(detail)
        self.pattern_scope = QLabel("")
        self.pattern_scope.setObjectName("taskSubtitle")
        self.pattern_scope.setAccessibleName(self.strings.text("pattern.next_scope"))
        self.right_layout.addWidget(self.pattern_scope)

    def _build_phase_buttons(self) -> None:
        self.phase_group = QButtonGroup(self)
        self.phase_group.setExclusive(True)
        self.phase_buttons: dict[str, QPushButton] = {}
        layout = QHBoxLayout()
        layout.setSpacing(5)
        for phase in PHASES:
            label = self.strings.text(f"phase.{phase}")
            button = QPushButton(label.upper())
            button.setToolTip(label)
            button.setCheckable(True)
            button.setProperty("phase", True)
            button.clicked.connect(
                lambda checked=False, value=phase: self.phase_selected.emit(value)
            )
            self.phase_group.addButton(button)
            self.phase_buttons[phase] = button
            layout.addWidget(button)
        self.right_layout.addLayout(layout)

    def _build_duration_controls(self, settings: dict[str, Any]) -> None:
        grid = QGridLayout()
        grid.setVerticalSpacing(7)
        self.duration_spins: dict[str, QSpinBox] = {}
        for row, phase in enumerate(PHASES):
            label_text = self.strings.text(f"phase.{phase}")
            label = QLabel(label_text)
            spin = QSpinBox()
            spin.setAccessibleName(
                self.strings.text("pattern.duration_accessible", phase=label_text)
            )
            spin.setRange(1, 180)
            spin.setSuffix(self.strings.text("pattern.minutes_suffix"))
            spin.setValue(int(settings["durations"][phase]))
            spin.valueChanged.connect(
                lambda value, key=phase: self.duration_changed.emit(key, value)
            )
            grid.addWidget(label, row, 0)
            grid.addWidget(spin, row, 1)
            self.duration_spins[phase] = spin
        self.right_layout.addLayout(grid)

    def _build_auto_break_controls(self, settings: dict[str, Any]) -> None:
        self.auto_breaks = QCheckBox(self.strings.text("pattern.auto_breaks"))
        self.auto_breaks.setChecked(bool(settings.get("autoStartBreaks")))
        self.auto_breaks.toggled.connect(self.auto_breaks_changed)
        self.right_layout.addWidget(self.auto_breaks)
        detail = QLabel(self.strings.text("pattern.auto_breaks_detail"))
        detail.setObjectName("taskSubtitle")
        detail.setWordWrap(True)
        self.right_layout.addWidget(detail)

    def _build_alert_notice(self) -> None:
        self.alert_guarantee = QLabel(self.strings.text("pattern.alert_guarantee"))
        self.alert_guarantee.setObjectName("privacyNotice")
        self.alert_guarantee.setWordWrap(True)
        self.alert_guarantee.setAccessibleName(
            self.strings.text("pattern.alert_accessible")
        )
        self.right_layout.addWidget(self.alert_guarantee)

    def set_settings_visible(self, visible: bool) -> None:
        self.right_panel.setVisible(visible)

    def apply_responsive_layout(self, *, landscape: bool, compact: bool) -> None:
        self.left_layout.setDirection(
            QBoxLayout.Direction.LeftToRight
            if landscape
            else QBoxLayout.Direction.TopToBottom
        )
        self.actions_layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if landscape
            else QBoxLayout.Direction.LeftToRight
        )
        self.content_layout.setSpacing(12 if compact else 22)
        self.left_layout.setSpacing(6 if compact else 10)
        margin = 12 if compact else 18
        self.right_layout.setContentsMargins(margin, margin, margin, margin)
        self.right_layout.setSpacing(7 if compact else 12)
        self.right_panel.setMinimumWidth(225 if compact else 310)
        self.right_panel.setMaximumWidth(300 if compact else 370)

    def render(
        self,
        source_timer: dict[str, Any],
        *,
        selected_phase: str,
        settings: dict[str, Any],
        now_ms: int,
        history: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
        mutations_enabled: bool,
        sound_active: bool,
    ) -> TimerRenderState:
        state = self.presentation(
            source_timer,
            selected_phase=selected_phase,
            settings=settings,
            now_ms=now_ms,
        )
        self.render_clock(state, history)
        self.render_task_selector(
            state.display_timer,
            state.active,
            selected_phase=selected_phase,
            settings=settings,
            tasks=tasks,
            known_tasks=known_tasks,
            mutations_enabled=mutations_enabled,
        )
        self.render_controls(
            state,
            selected_phase=selected_phase,
            mutations_enabled=mutations_enabled,
            sound_active=sound_active,
        )
        return state

    @staticmethod
    def presentation(
        source_timer: dict[str, Any],
        *,
        selected_phase: str,
        settings: dict[str, Any],
        now_ms: int,
    ) -> TimerRenderState:
        status = source_timer.get("status", "idle")
        timer = timer_for_display(
            source_timer,
            selected_phase,
            settings["durationsMs"],
        )
        elapsed = elapsed_ms(timer, now_ms)
        if status == "cancelled":
            elapsed = 0
        planned = max(1, int(timer["plannedDurationMs"]))
        return TimerRenderState(
            source_timer,
            timer,
            status,
            elapsed,
            planned,
            max(0, planned - elapsed),
        )

    def render_clock(
        self,
        state: TimerRenderState,
        history: list[dict[str, Any]],
    ) -> None:
        labels = self.status_labels()
        phase_label = self.strings.text(f"phase.{state.display_timer['phase']}")
        self.clock.set_state(
            format_remaining(state.remaining),
            phase_label,
            labels.get(state.status, state.status),
            state.elapsed / state.planned,
        )
        progress = long_break_progress(completed_focus_count_for_day(history))
        self.long_break_progress.setText("●" * progress + "○" * (4 - progress))
        self.long_break_progress.setAccessibleDescription(
            self.strings.text("status.pomodoro_progress_value", count=progress)
        )

    def render_controls(
        self,
        state: TimerRenderState,
        *,
        selected_phase: str,
        mutations_enabled: bool,
        sound_active: bool,
    ) -> None:
        self.primary_button.setText(
            self.strings.text("status.primary.pause")
            if state.status == "running"
            else self.strings.text("status.primary.resume")
            if state.status == "paused"
            else self.strings.text(
                "status.primary.start",
                phase=self.strings.text(f"phase.{selected_phase}").upper(),
            )
        )
        self.primary_button.setEnabled(
            mutations_enabled
            and state.status in {"idle"} | ACTIVE_STATUSES | TERMINAL_STATUSES
        )
        self.finish_button.setEnabled(mutations_enabled and state.active)
        self.cancel_button.setEnabled(mutations_enabled and state.active)
        self.stop_sound_button.setVisible(sound_active)
        self.stop_sound_button.setEnabled(sound_active)
        for spin in self.duration_spins.values():
            spin.setEnabled(mutations_enabled)
        for phase, button in self.phase_buttons.items():
            button.setChecked(phase == selected_phase)
            button.setEnabled(mutations_enabled)
        self.auto_breaks.setEnabled(mutations_enabled)
        self.pattern_scope.setText(
            self.strings.text("pattern.next_scope") if state.active else ""
        )

    @staticmethod
    def status_labels_for(strings: Strings) -> dict[str, str]:
        return {
            value: strings.text(f"status.rail.{value}")
            for value in (
                "idle",
                "running",
                "paused",
                "completed",
                "cancelled",
                "superseded",
            )
        }

    def status_labels(self) -> dict[str, str]:
        return self.status_labels_for(self.strings)

    def render_task_selector(
        self,
        timer: dict[str, Any],
        active: bool,
        *,
        selected_phase: str,
        settings: dict[str, Any],
        tasks: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
        mutations_enabled: bool,
    ) -> None:
        selected_task_id = (
            settings.get("selectedTaskId") if selected_phase == "focus" else None
        )
        active_task_id = timer.get("taskId") if active else None
        active_task = known_tasks.get(active_task_id) if active_task_id else None
        active_task_label = self._active_task_label(active_task, active_task_id)
        self.active_task_context.setText(
            self.strings.text("task.active_context", task=active_task_label)
            if active
            else ""
        )
        self.active_task_context.setVisible(active)
        choices = self._task_choices(tasks, known_tasks, selected_task_id)
        signature = self._selector_state(
            timer,
            selected_phase,
            selected_task_id,
            choices,
            mutations_enabled,
        )
        if signature == self._task_selector_signature:
            return
        self._task_selector_signature = signature
        self._populate_task_selector(choices, selected_task_id)
        self._configure_task_selector(
            active,
            active_task_label,
            selected_phase,
            mutations_enabled,
        )

    def invalidate_task_selector(self) -> None:
        self._task_selector_signature = None

    def _active_task_label(
        self,
        task: dict[str, Any] | None,
        task_id: str | None,
    ) -> str:
        if task:
            return task["title"]
        if task_id:
            return self.strings.text("task.deleted")
        return self.strings.text("task.unassigned")

    def _task_choices(
        self,
        tasks: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
        selected_task_id: str | None,
    ) -> list[dict[str, Any]]:
        choices = list(tasks)
        if selected_task_id and not any(
            task["id"] == selected_task_id for task in choices
        ):
            choices.append(
                known_tasks.get(selected_task_id)
                or {
                    "id": selected_task_id,
                    "title": self.strings.text("task.deleted"),
                }
            )
        return choices

    @staticmethod
    def _selector_state(
        timer: dict[str, Any],
        selected_phase: str,
        selected_task_id: str | None,
        choices: list[dict[str, Any]],
        mutations_enabled: bool,
    ) -> tuple[Any, ...]:
        return (
            timer.get("status"),
            selected_phase,
            selected_task_id,
            mutations_enabled,
            tuple((task["id"], task["title"]) for task in choices),
        )

    def _populate_task_selector(
        self,
        choices: list[dict[str, Any]],
        selected_task_id: str | None,
    ) -> None:
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem(self.strings.text("task.unassigned"), None)
        self._align_task_item(0)
        selected_index = 0
        for task in choices:
            self.task_combo.addItem(task["title"], task["id"])
            index = self.task_combo.count() - 1
            self._align_task_item(index)
            if task["id"] == selected_task_id:
                selected_index = index
        self.task_combo.setCurrentIndex(selected_index)
        self.task_combo.blockSignals(False)

    def _align_task_item(self, index: int) -> None:
        self.task_combo.setItemData(
            index,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            Qt.ItemDataRole.TextAlignmentRole,
        )

    def _configure_task_selector(
        self,
        active: bool,
        active_task_label: str,
        selected_phase: str,
        mutations_enabled: bool,
    ) -> None:
        self.task_combo.setEnabled(mutations_enabled and selected_phase == "focus")
        self.task_combo.setAccessibleName(
            self.strings.text("task.next_focus")
            if active
            else self.strings.text("task.focus")
        )
        description = (
            self.strings.text("task.next_description", task=active_task_label)
            if active
            else ""
        )
        self.task_combo.setAccessibleDescription(description)
        self.task_combo.setToolTip(description)

    def refresh_duration_spins(self, settings: dict[str, Any]) -> None:
        for phase, spin in self.duration_spins.items():
            previous = spin.blockSignals(True)
            spin.setValue(int(settings["durations"][phase]))
            spin.blockSignals(previous)

    def refresh_auto_breaks(self, settings: dict[str, Any]) -> None:
        previous = self.auto_breaks.blockSignals(True)
        self.auto_breaks.setChecked(bool(settings.get("autoStartBreaks")))
        self.auto_breaks.blockSignals(previous)

    @staticmethod
    def stylesheet() -> str:
        return """
        QPushButton[phase="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QFrame#taskSelector { background: palette(base); border: 2px solid palette(mid); }
        QSpinBox { min-width: 68px; font-family: "DejaVu Sans Mono"; }
        QCheckBox { spacing: 8px; }
        """
