from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QTimer

from .controller_outcomes import (
    ControllerOutcome,
    EmitNotice,
    LoadState,
    Render,
    ShowWindow,
    StopSound,
    Synchronize,
    done,
    returning,
)
from .core import (
    ACTIVE_STATUSES,
    PHASES,
    TERMINAL_STATUSES,
    elapsed_ms,
    empty_timer,
    task_from_title,
)


@dataclass(frozen=True, slots=True)
class TimerInteractionContext:
    store: Any
    cloud: Any
    closed: bool
    timer: dict[str, Any] | None
    settings: dict[str, Any]
    user: dict[str, Any] | None
    tasks: list[dict[str, Any]]
    known_tasks: dict[str, dict[str, Any]]
    projection_now_ms: int
    replication_mode: str
    history_resolution_active: bool


@dataclass(frozen=True, slots=True)
class TimerInteractionPorts:
    context: Callable[[], TimerInteractionContext]
    apply_outcome: Callable[[ControllerOutcome[Any]], None]
    mutation_blocked: Callable[[], bool]
    issue_command: Callable[[str, bool | None], None]
    maybe_auto_start_break: Callable[..., bool]
    notice: Callable[[str], None]
    task_input_text: Callable[[], str]
    clear_task_input: Callable[[], None]
    task_item_data: Callable[[int], Any]
    invalidate_task_selector: Callable[[], None]
    render_task_selector: Callable[[dict[str, Any], bool], None]
    refresh_duration_spins: Callable[[dict[str, Any]], None]
    refresh_auto_breaks: Callable[[dict[str, Any]], None]
    stop_sound_timer: Callable[[], None]
    stop_completion_sound: Callable[[], None]
    set_stop_sound_control: Callable[[bool], None]


class TimerInteractionController:
    """Owns timer commands, timer-driven mutations, and completion alert state."""

    def __init__(self, ports: TimerInteractionPorts) -> None:
        self._ports = ports
        self.notified_timer_id: str | None = None
        self.sound_active = False
        self.alert_timer_identity: tuple[object, object, object] | None = None
        self.auto_finish_in_progress = False
        self.auto_break_not_before = 0.0
        self.provisional_auto_break_timer_ids: set[str] = set()

    def _context(self) -> TimerInteractionContext:
        return self._ports.context()

    @staticmethod
    def _selected_phase_value(context: TimerInteractionContext) -> str:
        value = context.settings.get("selectedPhase", "focus")
        return value if value in PHASES else "focus"

    def selected_phase(self) -> ControllerOutcome[str]:
        return returning(self._selected_phase_value(self._context()))

    def _current_timer_value(
        self, context: TimerInteractionContext | None = None
    ) -> dict[str, Any]:
        current = context or self._context()
        phase = self._selected_phase_value(current)
        return current.timer or empty_timer(
            phase, int(current.settings["durationsMs"][phase])
        )

    def current_timer(self) -> ControllerOutcome[dict[str, Any]]:
        return returning(self._current_timer_value())

    def reconcile_loaded_timer(
        self,
        previous_timer: dict[str, Any] | None,
        provisional_timer_ids: set[str],
    ) -> ControllerOutcome[None]:
        context = self._context()
        current_alert_identity = (
            (
                context.timer.get("id"),
                context.timer.get("phase"),
                context.timer.get("status"),
            )
            if context.timer is not None
            else None
        )
        if self.sound_active and current_alert_identity != self.alert_timer_identity:
            self._ports.apply_outcome(done(StopSound()))
        self.provisional_auto_break_timer_ids = (
            provisional_timer_ids if context.user is not None else set()
        )
        if (
            previous_timer
            and context.timer
            and previous_timer.get("id") == context.timer.get("id")
            and (
                previous_timer.get("phase") != context.timer.get("phase")
                or previous_timer.get("status") in TERMINAL_STATUSES
                and context.timer.get("status") in ACTIVE_STATUSES
            )
        ):
            self.notified_timer_id = None
        return done()

    def tick(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.closed:
            return done()
        if (
            (context.user is None or not context.cloud.authenticated)
            and not context.cloud.busy
            and context.store.has_pending_auto_break()
        ):
            self._ports.maybe_auto_start_break(require_canonical=False)
            context = self._context()
        timer = self._current_timer_value(context)
        context, timer = self._refresh_expired_timer(context, timer)
        if timer.get("status") not in {"running", "completed"}:
            return done()
        if context.history_resolution_active:
            return done(Render())
        deadline_completed = (
            timer.get("status") == "completed"
            and timer.get("lastIntent", {}).get("type") != "finish"
        )
        if deadline_completed:
            return self._complete_expired_timer(context, timer)
        return done(Render()) if timer.get("status") == "running" else done()

    def _refresh_expired_timer(
        self,
        context: TimerInteractionContext,
        timer: dict[str, Any],
    ) -> tuple[TimerInteractionContext, dict[str, Any]]:
        if timer.get("status") == "running" and elapsed_ms(
            timer,
            context.store.effective_timer_now_ms(timer),
        ) >= int(timer["plannedDurationMs"]):
            self._ports.apply_outcome(done(LoadState()))
            context = self._context()
            timer = self._current_timer_value(context)
        return context, timer

    def _complete_expired_timer(
        self,
        context: TimerInteractionContext,
        timer: dict[str, Any],
    ) -> ControllerOutcome[None]:
        if self.auto_finish_in_progress:
            return done()
        self.auto_finish_in_progress = True
        if context.replication_mode == "iroh":
            try:
                context.store.project_iroh_expiry(context.projection_now_ms)
            except (OSError, ValueError) as error:
                self._ports.notice(str(error))
            self.auto_finish_in_progress = False
            return done(LoadState(), Render(), Synchronize())
        self._ports.issue_command("finish", True)
        return done()

    def primary_action(self) -> ControllerOutcome[None]:
        context = self._context()
        status = self._current_timer_value(context).get("status")
        if status == "running":
            self._ports.issue_command("pause", None)
        elif status == "paused":
            self._ports.issue_command("resume", None)
        elif status == "idle":
            self._ports.issue_command("start", None)
        elif status in TERMINAL_STATUSES:
            if self._ports.mutation_blocked():
                return done()
            try:
                context.store.queue_restart(
                    context.timer,
                    self._selected_phase_value(context),
                    context.settings["durationsMs"],
                    context.settings.get("selectedTaskId"),
                )
            except (OSError, ValueError) as error:
                return done(EmitNotice(str(error)))
            return done(LoadState(), Render(), Synchronize())
        return done()

    def issue(
        self, command_type: str, automatic: bool = False
    ) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            self.auto_finish_in_progress = False
            return done()
        context = self._context()
        timer = self._current_timer_value(context)
        if not self.command_is_valid(command_type, timer):
            self.auto_finish_in_progress = False
            return done()
        try:
            queued = self._queue_timer_command_value(context, command_type, automatic)
        except (OSError, ValueError) as error:
            self.auto_finish_in_progress = False
            return done(EmitNotice(str(error)))
        if not queued:
            self.auto_finish_in_progress = False
            return done(LoadState(), Render())
        return self.after_timer_command(command_type, automatic)

    @staticmethod
    def command_is_valid(command_type: str, timer: dict[str, Any]) -> bool:
        status = timer.get("status")
        valid = {
            "start": status == "idle",
            "pause": status == "running",
            "resume": status == "paused",
            "finish": status in ACTIVE_STATUSES
            or status == "completed"
            and timer.get("lastIntent", {}).get("type") != "finish",
            "cancel": status in ACTIVE_STATUSES,
            "clear": status in TERMINAL_STATUSES,
        }
        return valid.get(command_type, False)

    def _queue_timer_command_value(
        self,
        context: TimerInteractionContext,
        command_type: str,
        automatic: bool,
    ) -> bool:
        if command_type == "cancel":
            context.store.queue_cancel_and_clear(
                context.timer,
                self._selected_phase_value(context),
                context.settings["durationsMs"],
                context.settings.get("selectedTaskId"),
            )
            return True
        command = context.store.queue_command(
            command_type,
            context.timer,
            self._selected_phase_value(context),
            context.settings["durationsMs"],
            context.settings.get("selectedTaskId"),
            now_ms=context.projection_now_ms if automatic else None,
            automatic=automatic,
        )
        return not automatic or command is not None

    def queue_timer_command(
        self, command_type: str, automatic: bool
    ) -> ControllerOutcome[bool]:
        context = self._context()
        return returning(
            self._queue_timer_command_value(context, command_type, automatic)
        )

    def after_timer_command(
        self, command_type: str, automatic: bool
    ) -> ControllerOutcome[None]:
        self._ports.apply_outcome(done(LoadState(), Render(), Synchronize()))
        if command_type == "finish":
            self.auto_finish_in_progress = False
            context = self._context()
            if context.timer and context.timer.get("phase") == "focus":
                self.auto_break_not_before = time.monotonic() + 1.2
                QTimer.singleShot(1200, self._ports.maybe_auto_start_break)
        return done(ShowWindow()) if automatic else done()

    def maybe_auto_start_break(
        self,
        *,
        sync: bool = True,
        allow_busy: bool = False,
        require_canonical: bool | None = None,
    ) -> ControllerOutcome[bool]:
        context = self._context()
        if (
            context.closed
            or time.monotonic() < self.auto_break_not_before
            or context.history_resolution_active
            or (context.cloud.busy and not allow_busy)
        ):
            return returning(False)
        try:
            commands = context.store.process_auto_break(
                require_canonical=(
                    context.user is not None and context.cloud.authenticated
                    if require_canonical is None
                    else require_canonical
                )
            )
        except (OSError, ValueError) as error:
            return returning(False, EmitNotice(str(error)))
        if not commands:
            return returning(False)
        effects = [LoadState(), Render()]
        if sync:
            effects.append(Synchronize())
        return returning(True, *effects)

    def select_phase(self, phase: str) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            return done(Render())
        context = self._context()
        context.settings["selectedPhase"] = phase
        context.store.set_selected_phase(phase)
        return done(Render())

    def task_selection_changed(self, index: int) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            self._ports.invalidate_task_selector()
            timer = self._current_timer_value()
            self._ports.render_task_selector(
                timer, timer.get("status") in ACTIVE_STATUSES
            )
            return done()
        context = self._context()
        task_id = self._ports.task_item_data(index)
        if task_id and not any(task["id"] == task_id for task in context.tasks):
            task_id = None
        try:
            context.store.set_selected_task_id(task_id)
        except (OSError, ValueError) as error:
            self._ports.apply_outcome(done(LoadState()))
            self._ports.invalidate_task_selector()
            return done(Render(), EmitNotice(str(error)))
        return done(LoadState(), Render(), Synchronize())

    def add_task(self, requested_title: str | None = None) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            return done()
        context = self._context()
        try:
            title = (
                requested_title
                if isinstance(requested_title, str)
                else self._ports.task_input_text()
            )
            task = task_from_title(title)
            if not any(existing["id"] == task["id"] for existing in context.tasks):
                context.store.queue_task_operation("upsert", task)
            context.settings["selectedTaskId"] = task["id"]
            context.store.set_selected_task_id(task["id"])
        except (OSError, ValueError) as error:
            return done(EmitNotice(str(error)))
        self._ports.clear_task_input()
        return done(LoadState(), Render(), Synchronize())

    def delete_task(self, task_id: str) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            return done()
        context = self._context()
        task = context.known_tasks.get(task_id)
        if not task:
            return done()
        try:
            context.store.queue_task_operation("delete", task)
            if context.settings.get("selectedTaskId") == task_id:
                context.settings["selectedTaskId"] = None
                context.store.set_selected_task_id(None)
        except (OSError, ValueError) as error:
            return done(EmitNotice(str(error)))
        return done(LoadState(), Render(), Synchronize())

    def duration_changed(self, phase: str, value: int) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            self._ports.refresh_duration_spins(self._context().settings)
            return done()
        context = self._context()
        try:
            context.store.queue_duration_operation(phase, value * 60_000)
        except (OSError, ValueError) as error:
            return done(LoadState(), EmitNotice(str(error)))
        self._ports.apply_outcome(done(LoadState()))
        context = self._context()
        effects: list[Any] = []
        if not context.timer and phase == self._selected_phase_value(context):
            effects.append(Render())
        effects.append(Synchronize())
        return done(*effects)

    def auto_breaks_changed(self, enabled: bool) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            self._ports.refresh_auto_breaks(self._context().settings)
            return done()
        context = self._context()
        try:
            context.store.set_auto_start_breaks(enabled)
        except (OSError, ValueError) as error:
            self._ports.apply_outcome(done(LoadState()))
            self._ports.refresh_auto_breaks(self._context().settings)
            return done(EmitNotice(str(error)))
        return done(LoadState(), Synchronize())

    def schedule_pending_auto_break(
        self, *, require_canonical: bool | None = None
    ) -> ControllerOutcome[None]:
        if self._context().store.has_pending_auto_break():
            delay_ms = max(
                0,
                math.ceil((self.auto_break_not_before - time.monotonic()) * 1000),
            )
            QTimer.singleShot(
                delay_ms,
                lambda: self._ports.maybe_auto_start_break(
                    allow_busy=True, require_canonical=require_canonical
                ),
            )
        return done()

    def stop_sound(self) -> ControllerOutcome[None]:
        self._ports.stop_sound_timer()
        self._ports.stop_completion_sound()
        self.sound_active = False
        self.alert_timer_identity = None
        self._ports.set_stop_sound_control(False)
        return done()

    def stop_sound_and_clear(self) -> ControllerOutcome[None]:
        status = self._current_timer_value().get("status")
        self._ports.apply_outcome(self.stop_sound())
        if status in TERMINAL_STATUSES:
            self._ports.issue_command("clear", None)
        return done()
