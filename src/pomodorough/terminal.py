from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .core import (
    ACTIVE_STATUSES,
    PHASES,
    TERMINAL_STATUSES,
    elapsed_ms,
    empty_timer,
    format_remaining,
    timer_for_display,
)
from .localization import Strings
from .storage import Store

PHASE_ALIASES = {
    "focus": "focus",
    "short": "short_break",
    "short-break": "short_break",
    "short_break": "short_break",
    "long": "long_break",
    "long-break": "long_break",
    "long_break": "long_break",
}


class InvalidAction(ValueError):
    pass


def normalize_phase(value: str, strings: Strings | None = None) -> str:
    try:
        return PHASE_ALIASES[value.lower()]
    except KeyError as error:
        strings = strings or Strings()
        raise InvalidAction(strings.text("error.unknown_phase", value=value)) from error


class LocalTimer:
    """Terminal-facing timer operations backed by the shared local store."""

    def __init__(self, store: Store, *, strings: Strings | None = None) -> None:
        self.store = store
        self.strings = strings or Strings()
        self.settings: dict[str, Any] = {}
        self.timer: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.known_tasks: dict[str, dict[str, Any]] = {}
        self.pending: list[dict[str, Any]] = []
        self.pending_durations: list[dict[str, Any]] = []
        self.pending_auto_starts: list[dict[str, Any]] = []
        self.resolution_pending = False
        self.reload()

    def reload(self, *, now_ms: int | None = None) -> None:
        if now_ms is None:
            physical_now_ms = int(time.time() * 1_000)
            now_ms = self.store.effective_timer_now_ms(
                self.timer,
                physical_ms=physical_now_ms,
            )
        state = self.store.load(projection=True)
        projection = self.store.projected_state(now_ms=now_ms, state=state)
        self.settings = self.store.projected_settings(state, projection)
        snapshot = state["snapshot"]
        self.pending = state["pending"]
        self.pending_durations = state["pendingDurations"]
        self.pending_auto_starts = state["pendingAutoStarts"]
        self.resolution_pending = state["pendingResolution"] is not None
        self.tasks = projection.tasks
        self.known_tasks = {
            task["id"]: task
            for task in snapshot.get("knownTasks", [])
            if task.get("id") and task.get("title")
        }
        for task in self.tasks:
            self.known_tasks[task["id"]] = task
        self.timer = projection.canonical_timer
        self.history = self.store.projected_history(projection, state)

    @property
    def selected_phase(self) -> str:
        phase = self.settings.get("selectedPhase", "focus")
        return phase if phase in PHASES else "focus"

    def current_timer(self) -> dict[str, Any]:
        phase = self.selected_phase
        duration_ms = int(self.settings["durationsMs"][phase])
        return self.timer or empty_timer(phase, duration_ms)

    def state(
        self, now_ms: int | None = None, auto_finish: bool = False
    ) -> dict[str, Any]:
        supplied_now_ms, physical_now_ms, projection_now_ms = self._state_clock(
            now_ms
        )
        self.reload(now_ms=projection_now_ms)
        self._process_pending_auto_break(
            supplied_now_ms,
            physical_now_ms,
            projection_now_ms,
        )
        timer = self.current_timer()
        status = timer.get("status", "idle")
        display_timer = timer_for_display(
            timer,
            self.selected_phase,
            self.settings["durationsMs"],
        )
        canonical_elapsed = elapsed_ms(timer, projection_now_ms)
        if status == "cancelled":
            canonical_elapsed = 0
        canonical_planned = max(1, int(timer["plannedDurationMs"]))
        completed_state = self._finish_completed_timer(
            timer,
            projection_now_ms,
            auto_finish,
        )
        if completed_state is not None:
            return completed_state
        return self._state_document(
            timer,
            display_timer,
            projection_now_ms,
            canonical_elapsed,
            canonical_planned,
        )

    def _state_clock(self, now_ms: int | None) -> tuple[bool, int, int]:
        supplied_now_ms = now_ms is not None
        physical_now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        projection_now_ms = (
            physical_now_ms
            if supplied_now_ms
            else self.store.effective_timer_now_ms(
                self.timer,
                physical_ms=physical_now_ms,
            )
        )
        return supplied_now_ms, physical_now_ms, projection_now_ms

    def _process_pending_auto_break(
        self,
        supplied_now_ms: bool,
        physical_now_ms: int,
        projection_now_ms: int,
    ) -> None:
        if not self.resolution_pending and self.store.has_pending_auto_break():
            self._store_action(
                lambda: self.store.process_auto_break(
                    require_canonical=False,
                    now_ms=physical_now_ms if supplied_now_ms else None,
                )
            )
            self.reload(now_ms=projection_now_ms)

    def _finish_completed_timer(
        self,
        timer: dict[str, Any],
        projection_now_ms: int,
        auto_finish: bool,
    ) -> dict[str, Any] | None:
        status = timer.get("status", "idle")
        should_finish = (
            auto_finish
            and not self.resolution_pending
            and status == "completed"
            and timer.get("lastIntent", {}).get("type") != "finish"
        )
        if not should_finish:
            return None
        if self.store.replication_mode == "iroh":
            self._store_action(
                lambda: self.store.project_iroh_expiry(projection_now_ms)
            )
            return self.state(now_ms=projection_now_ms)
        self.issue(
            "finish",
            now_ms=projection_now_ms,
            automatic=True,
        )
        return self.state(now_ms=projection_now_ms)

    def _timer_metrics(
        self,
        timer: dict[str, Any],
        now_ms: int,
        *,
        elapsed: int | None = None,
        planned: int | None = None,
    ) -> dict[str, Any]:
        elapsed = elapsed_ms(timer, now_ms) if elapsed is None else elapsed
        planned = (
            max(1, int(timer["plannedDurationMs"]))
            if planned is None
            else planned
        )
        remaining = max(0, planned - elapsed)
        task_id = timer.get("taskId")
        task = self.known_tasks.get(task_id) if isinstance(task_id, str) else None
        return {
            "phase": timer["phase"],
            "phaseLabel": PHASES[timer["phase"]]["label"],
            "taskId": task_id,
            "taskTitle": task.get("title") if task else None,
            "plannedDurationMs": planned,
            "elapsedMs": elapsed,
            "remainingMs": remaining,
            "remaining": format_remaining(remaining),
            "progress": min(1.0, elapsed / planned),
        }

    def _state_document(
        self,
        timer: dict[str, Any],
        display_timer: dict[str, Any],
        projection_now_ms: int,
        canonical_elapsed: int,
        canonical_planned: int,
    ) -> dict[str, Any]:
        metrics = self._timer_metrics(
            timer,
            projection_now_ms,
            elapsed=canonical_elapsed,
            planned=canonical_planned,
        )
        return {
            "timerId": timer.get("id") or None,
            "phase": metrics["phase"],
            "phaseLabel": metrics["phaseLabel"],
            "status": timer.get("status", "idle"),
            "taskId": metrics["taskId"],
            "taskTitle": metrics["taskTitle"],
            "plannedDurationMs": metrics["plannedDurationMs"],
            "elapsedMs": metrics["elapsedMs"],
            "remainingMs": metrics["remainingMs"],
            "remaining": metrics["remaining"],
            "progress": metrics["progress"],
            "display": self._timer_metrics(display_timer, projection_now_ms),
            "pendingCommands": len(self.pending),
            "pendingDurationOperations": len(self.pending_durations),
            "pendingAutoStartOperations": len(self.pending_auto_starts),
            "historyResolutionPending": self.resolution_pending,
        }

    def _store_action(self, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except ValueError as error:
            self.reload()
            raise InvalidAction(str(error)) from error

    def issue(
        self,
        command_type: str,
        phase: str | None = None,
        minutes: int | None = None,
        now_ms: int | None = None,
        automatic: bool = False,
    ) -> dict[str, Any] | None:
        self.reload(now_ms=now_ms)
        timer = self.current_timer()
        command_type = self._validated_command(command_type, timer)
        selected_phase, durations_ms = self._command_settings(phase, minutes)
        self._select_start_phase(command_type, selected_phase)
        command = self._queue_command(
            command_type,
            selected_phase,
            durations_ms,
            now_ms,
            automatic,
        )
        self.reload(now_ms=now_ms)
        return command

    def _validated_command(
        self, command_type: str, timer: dict[str, Any]
    ) -> str:
        requested_action = command_type
        command_type = "clear" if command_type == "dismiss" else command_type
        status = timer.get("status", "idle")
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
        if command_type not in valid:
            raise InvalidAction(
                self.strings.text("error.unknown_action", action=command_type)
            )
        if not valid[command_type]:
            raise InvalidAction(
                self.strings.text(
                    "error.invalid_transition", action=requested_action, status=status
                )
            )
        return command_type

    def _command_settings(
        self, phase: str | None, minutes: int | None
    ) -> tuple[str, dict[str, Any]]:
        selected_phase = (
            normalize_phase(phase, self.strings) if phase else self.selected_phase
        )
        durations_ms = deepcopy(self.settings["durationsMs"])
        if minutes is not None:
            if not 1 <= minutes <= 180:
                raise InvalidAction(self.strings.text("error.duration_range"))
            durations_ms[selected_phase] = minutes * 60_000
        return selected_phase, durations_ms

    def _select_start_phase(self, command_type: str, selected_phase: str) -> None:
        if command_type == "start" and selected_phase != self.selected_phase:
            self._store_action(
                lambda: self.store.set_selected_phase(selected_phase)
            )
            self.settings["selectedPhase"] = selected_phase

    def _queue_command(
        self,
        command_type: str,
        selected_phase: str,
        durations_ms: dict[str, Any],
        now_ms: int | None,
        automatic: bool,
    ) -> dict[str, Any] | None:
        if command_type == "cancel":
            commands = self._store_action(
                lambda: self.store.queue_cancel_and_clear(
                    self.timer,
                    selected_phase,
                    durations_ms,
                    self.settings.get("selectedTaskId"),
                    now_ms=now_ms,
                )
            )
            command = commands[-1]
        else:
            command = self._store_action(
                lambda: self.store.queue_command(
                    command_type,
                    self.timer,
                    selected_phase,
                    durations_ms,
                    self.settings.get("selectedTaskId"),
                    now_ms=now_ms,
                    generate_auto_break=command_type == "finish",
                    automatic=automatic,
                )
            )
        return command

    def primary(self, now_ms: int | None = None) -> None:
        self.reload(now_ms=now_ms)
        status = self.current_timer().get("status", "idle")
        if status == "running":
            self.issue("pause", now_ms=now_ms)
        elif status == "paused":
            self.issue("resume", now_ms=now_ms)
        elif status == "idle":
            self.issue("start", now_ms=now_ms)
        elif status in TERMINAL_STATUSES:
            commands = self._store_action(
                lambda: self.store.queue_restart(
                    self.timer,
                    self.selected_phase,
                    deepcopy(self.settings["durationsMs"]),
                    self.settings.get("selectedTaskId"),
                    now_ms=now_ms,
                )
            )
            self.reload()
            return commands[-1]

    def select_phase(self, phase: str) -> None:
        self.reload()
        if self.current_timer().get("status") in ACTIVE_STATUSES:
            raise InvalidAction(self.strings.text("error.phase_active"))
        selected_phase = normalize_phase(phase, self.strings)
        self._store_action(lambda: self.store.set_selected_phase(selected_phase))
        self.reload()

    def adjust_duration(self, delta: int) -> int:
        self.reload()
        phase = self.selected_phase
        current = int(self.settings["durations"][phase])
        updated = min(180, max(1, current + delta))
        if updated == current:
            return current
        self._store_action(
            lambda: self.store.queue_duration_operation(phase, updated * 60_000)
        )
        self.reload()
        return updated

    def _task_context(self, task_id: object) -> tuple[str, str]:
        if isinstance(task_id, str) and task_id:
            task = self.known_tasks.get(task_id)
            if task:
                return "retained", str(task["title"])
            return "deleted", self.strings.text("task.deleted")
        return "unassigned", self.strings.text("task.unassigned")

    def retained_history(
        self, limit: int | None = None, *, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        self.reload(now_ms=now_ms)
        retained = []
        for item in self.history:
            if item.get("status") not in TERMINAL_STATUSES:
                continue
            result = dict(item)
            context, title = self._task_context(item.get("taskId"))
            result["taskContext"] = context
            result["taskTitle"] = title
            retained.append(result)
        return retained if limit is None else retained[:limit]

    def completed_history(
        self, limit: int | None = None, *, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Compatibility alias for callers predating retained terminal history."""
        return self.retained_history(limit, now_ms=now_ms)
