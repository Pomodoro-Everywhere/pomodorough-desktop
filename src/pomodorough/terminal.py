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
    rebuild_optimistic,
    rebuild_tasks,
)
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


def normalize_phase(value: str) -> str:
    try:
        return PHASE_ALIASES[value.lower()]
    except KeyError as error:
        choices = ", ".join(("focus", "short-break", "long-break"))
        raise InvalidAction(f"Unknown phase {value!r}. Choose {choices}.") from error


class LocalTimer:
    """Terminal-facing timer operations backed by the shared local store."""

    def __init__(self, store: Store) -> None:
        self.store = store
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

    def reload(self) -> None:
        state = self.store.load()
        self.settings = state["settings"]
        snapshot = state["snapshot"]
        self.pending = state["pending"]
        self.pending_durations = state["pendingDurations"]
        self.pending_auto_starts = state["pendingAutoStarts"]
        self.resolution_pending = state["pendingResolution"] is not None
        self.tasks = rebuild_tasks(
            snapshot.get("tasks", []), state.get("pendingTasks", [])
        )
        self.known_tasks = {
            task["id"]: task
            for task in snapshot.get("knownTasks", [])
            if task.get("id") and task.get("title")
        }
        for task in self.tasks:
            self.known_tasks[task["id"]] = task
        selected_task_id = self.settings.get("selectedTaskId")
        if selected_task_id and not any(
            task["id"] == selected_task_id for task in self.tasks
        ):
            self.settings["selectedTaskId"] = None
            if not self.resolution_pending:
                try:
                    self.store.set_selected_task_id(None)
                except ValueError:
                    self.resolution_pending = (
                        self.store.pending_resolution() is not None
                    )
        self.timer, self.history = rebuild_optimistic(
            snapshot.get("canonicalTimer"),
            snapshot.get("history", []),
            self.pending,
        )

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
        self.reload()
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if not self.resolution_pending and self.store.has_pending_auto_break():
            self._store_action(
                lambda: self.store.process_auto_break(
                    require_canonical=False,
                    now_ms=now_ms,
                )
            )
            self.reload()
        timer = self.current_timer()
        elapsed = elapsed_ms(timer, now_ms)
        if timer.get("status") == "cancelled":
            elapsed = 0
        planned = max(1, int(timer["plannedDurationMs"]))

        if (
            auto_finish
            and not self.resolution_pending
            and timer.get("status") == "running"
            and elapsed >= planned
        ):
            self.issue("finish", now_ms=now_ms)
            return self.state(now_ms=now_ms)

        remaining = max(0, planned - elapsed)
        task_id = timer.get("taskId")
        task = self.known_tasks.get(task_id)
        return {
            "timerId": timer.get("id") or None,
            "phase": timer["phase"],
            "phaseLabel": PHASES[timer["phase"]]["label"],
            "status": timer.get("status", "idle"),
            "taskId": task_id,
            "taskTitle": task.get("title") if task else None,
            "plannedDurationMs": planned,
            "elapsedMs": elapsed,
            "remainingMs": remaining,
            "remaining": format_remaining(remaining),
            "progress": min(1.0, elapsed / planned),
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
    ) -> dict[str, Any]:
        self.reload()
        timer = self.current_timer()
        status = timer.get("status", "idle")
        valid = {
            "start": status == "idle",
            "pause": status == "running",
            "resume": status == "paused",
            "finish": status in ACTIVE_STATUSES,
            "cancel": status in ACTIVE_STATUSES,
            "clear": status in TERMINAL_STATUSES,
        }
        if command_type not in valid:
            raise InvalidAction(f"Unknown timer action {command_type!r}.")
        if not valid[command_type]:
            raise InvalidAction(f"Cannot {command_type} timer while it is {status}.")

        selected_phase = normalize_phase(phase) if phase else self.selected_phase
        durations_ms = deepcopy(self.settings["durationsMs"])
        if minutes is not None:
            if not 1 <= minutes <= 180:
                raise InvalidAction("Duration must be between 1 and 180 minutes.")
            durations_ms[selected_phase] = minutes * 60_000

        if command_type == "start" and selected_phase != self.selected_phase:
            self._store_action(
                lambda: self.store.set_selected_phase(selected_phase)
            )
            self.settings["selectedPhase"] = selected_phase

        command = self._store_action(
            lambda: self.store.queue_command(
                command_type,
                self.timer,
                selected_phase,
                durations_ms,
                self.settings.get("selectedTaskId"),
                now_ms=now_ms,
            )
        )
        if command_type == "finish":
            self._store_action(
                lambda: self.store.process_auto_break(
                    require_canonical=False,
                    now_ms=now_ms,
                )
            )
        self.reload()
        return command

    def primary(self, now_ms: int | None = None) -> None:
        self.reload()
        status = self.current_timer().get("status", "idle")
        if status == "running":
            self.issue("pause", now_ms=now_ms)
        elif status == "paused":
            self.issue("resume", now_ms=now_ms)
        elif status == "idle":
            self.issue("start", now_ms=now_ms)
        elif status in TERMINAL_STATUSES:
            self.issue("clear", now_ms=now_ms)
            self.issue("start", now_ms=now_ms)

    def select_phase(self, phase: str) -> None:
        self.reload()
        if self.current_timer().get("status") in ACTIVE_STATUSES:
            raise InvalidAction("Cannot change phase while timer is active.")
        selected_phase = normalize_phase(phase)
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

    def completed_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.reload()
        completed = []
        for item in self.history:
            if item.get("status") != "completed":
                continue
            result = dict(item)
            task = self.known_tasks.get(item.get("taskId"))
            result["taskTitle"] = task.get("title") if task else None
            completed.append(result)
        return completed if limit is None else completed[:limit]
