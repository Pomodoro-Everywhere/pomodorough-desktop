from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QTimer

from .controller_outcomes import (
    ControllerOutcome,
    EmitFailure,
    EmitTaskFailure,
    LoadState,
    Render,
    ShowWindow,
    StopSound,
    Synchronize,
    TaskChange,
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
from .pending_task_deletion import (
    clear_pending_task_deletion,
    parse_pending_task_deletion,
    record_pending_task_deletion,
    stored_pending_task_deletion,
    workspace_owner_id,
)
from .sentry_monitoring import (
    capture_exception,
    note_clock_recovery,
    note_pending_break_read_recovery,
    should_capture_clock_failure,
    should_capture_pending_break_read_failure,
)
from .shared_core import SharedCoreError
from .task_draft_policy import reject_blank_task_draft

_LOGGER = logging.getLogger(__name__)


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
    # Ends the Tasks undo window so a deferred delete completes before a
    # user timer action could attach the hidden task.
    complete_pending_task_delete: Callable[[], None]
    # True when a failed or refused task delete is shown to the user at once,
    # with the window in view; False while hiding, quitting or replaying.
    # Doubles without a window show every failure, so they default to True.
    task_delete_attended: Callable[[], bool] = lambda: True


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
        # One-shot: the first reconcile after construction is the cold-start
        # load. Later reconciles are in-session refreshes and must never
        # pre-mark anything notified.
        self._startup_reconcile_pending = True
        # R50-D02: bounds repeat pending-break notices for one incident.
        # Captures use the independent sentry budget; success clears both.
        self._pending_break_notice_incident: str | None = None
        # The task whose delete is recorded durably and not yet completed.
        self._pending_task_delete_id: str | None = None
        # The workspace owner the delete was recorded in; None is local.
        self._pending_task_delete_owner: str | None = None
        self._task_delete_attempts = 0
        # M2: a delete that ended (Undo, or a failure the user saw) while its
        # marker could not be cleared. The clear is retried before the
        # workspace changes hands and at shutdown.
        self._withdrawn_task_delete_id: str | None = None

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
        startup_reconcile, self._startup_reconcile_pending = (
            self._startup_reconcile_pending,
            False,
        )
        if (
            startup_reconcile
            and previous_timer is None
            and context.timer is not None
            and context.timer.get("status") == "completed"
            and (context.timer.get("lastIntent") or {}).get("type") == "finish"
        ):
            # Cold-start load only (first reconcile): a timer finished by an
            # explicit finish intent was already processed (and alerted) in a
            # past session, so mark it notified instead of replaying its
            # completion alert/sound. Completions by expiry still alert, and
            # in-session reloads must still alert for fresh completions.
            self.notified_timer_id = context.timer.get("id")
        return done()

    @staticmethod
    def _pending_break_notice_signature(error: BaseException) -> str:
        return f"{type(error).__module__}.{type(error).__qualname__}"

    def _pending_break_notice_due(self, error: BaseException) -> bool:
        signature = self._pending_break_notice_signature(error)
        if signature == self._pending_break_notice_incident:
            return False
        self._pending_break_notice_incident = signature
        return True

    def _clear_pending_break_incident(self) -> None:
        note_pending_break_read_recovery()
        self._pending_break_notice_incident = None

    def _safe_has_pending_auto_break(
        self, context: TimerInteractionContext
    ) -> tuple[bool, EmitFailure | None]:
        try:
            pending = context.store.has_pending_auto_break()
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            if should_capture_pending_break_read_failure(error):
                capture_exception(error)
            if self._pending_break_notice_due(error):
                return False, EmitFailure(error)
            return False, None
        except ValueError as error:
            if self._pending_break_notice_due(error):
                return False, EmitFailure(error)
            return False, None
        self._clear_pending_break_incident()
        return bool(pending), None

    def tick(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.closed:
            return done()
        pending_notice: EmitFailure | None = None
        if (context.user is None or not context.cloud.authenticated) and not context.cloud.busy:
            pending_break, pending_notice = self._safe_has_pending_auto_break(
                context
            )
            if pending_break:
                self._ports.maybe_auto_start_break(require_canonical=False)
                context = self._context()
        timer = self._current_timer_value(context)
        context, timer = self._refresh_expired_timer(context, timer)
        if timer.get("status") not in {"running", "completed"}:
            return done() if pending_notice is None else done(pending_notice)
        if context.history_resolution_active:
            if pending_notice is None:
                return done(Render())
            return done(Render(), pending_notice)
        deadline_completed = (
            timer.get("status") == "completed"
            and timer.get("lastIntent", {}).get("type") != "finish"
        )
        if deadline_completed:
            outcome = self._complete_expired_timer(context, timer)
            if pending_notice is None:
                return outcome
            return done(*outcome.effects, pending_notice)
        if timer.get("status") == "running":
            if pending_notice is None:
                return done(Render())
            return done(Render(), pending_notice)
        return done() if pending_notice is None else done(pending_notice)

    def _refresh_expired_timer(
        self,
        context: TimerInteractionContext,
        timer: dict[str, Any],
    ) -> tuple[TimerInteractionContext, dict[str, Any]]:
        if timer.get("status") != "running":
            return context, timer
        try:
            now_ms = context.store.effective_timer_now_ms(timer)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D48-07: shared tick/render clock budget; keep last-known.
            if should_capture_clock_failure(error):
                capture_exception(error)
            return context, timer
        except ValueError:
            return context, timer
        note_clock_recovery()
        if elapsed_ms(timer, now_ms) >= int(timer["plannedDurationMs"]):
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
            except (OSError, sqlite3.Error, SharedCoreError) as error:
                # D41: infra failure reports to Sentry; failure omits
                # Synchronize so it is not masked as success.
                capture_exception(error)
                self.auto_finish_in_progress = False
                return done(LoadState(), Render(), EmitFailure(error))
            except ValueError as error:
                self.auto_finish_in_progress = False
                return done(LoadState(), Render(), EmitFailure(error))
            self.auto_finish_in_progress = False
            return done(LoadState(), Render(), Synchronize())
        self._ports.issue_command("finish", True)
        return done()

    def primary_action(self) -> ControllerOutcome[None]:
        selected_task_usable = self._complete_pending_task_delete()
        context = self._context()
        status = self._current_timer_value(context).get("status")
        if not selected_task_usable and status not in ACTIVE_STATUSES:
            return done()
        if status == "running":
            self._ports.issue_command("pause", None)
        elif status == "paused":
            self._ports.issue_command("resume", None)
        elif status == "idle":
            self._ports.issue_command("start", None)
        elif status in TERMINAL_STATUSES:
            context = self._claim_unfinished_completion(context)
            if self._ports.mutation_blocked():
                return done()
            try:
                context.store.queue_restart(
                    context.timer,
                    self._selected_phase_value(context),
                    context.settings["durationsMs"],
                    context.settings.get("selectedTaskId"),
                )
            except (OSError, sqlite3.Error, SharedCoreError) as error:
                # D41: infra failure reports to Sentry, still notice-only.
                capture_exception(error)
                return done(EmitFailure(error, self.primary_action))
            except ValueError as error:
                return done(EmitFailure(error))
            return done(LoadState(), Render(), Synchronize())
        return done()

    def _claim_unfinished_completion(
        self, context: TimerInteractionContext
    ) -> TimerInteractionContext:
        timer = self._current_timer_value(context)
        if timer.get("status") != "completed":
            return context
        if (timer.get("lastIntent") or {}).get("type") == "finish":
            return context
        self._ports.issue_command("finish", False)
        return self._context()

    def issue(
        self, command_type: str, automatic: bool = False
    ) -> ControllerOutcome[None]:
        if not automatic and not self._complete_pending_task_delete():
            if command_type == "start":
                return done()
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
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            self.auto_finish_in_progress = False
            retry = None if automatic else (lambda: self.issue(command_type))
            return done(EmitFailure(error, retry))
        except ValueError as error:
            self.auto_finish_in_progress = False
            return done(EmitFailure(error))
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
        try:
            queued = self._queue_timer_command_value(
                context, command_type, automatic
            )
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D43: direct dispatch bypasses issue() guard; same split as D41.
            capture_exception(error)
            return returning(False, EmitFailure(error))
        except ValueError as error:
            return returning(False, EmitFailure(error))
        return returning(queued)

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
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return returning(False, EmitFailure(error))
        except ValueError as error:
            return returning(False, EmitFailure(error))
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
        try:
            context.store.set_selected_phase(phase)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D42: same OSError/sqlite3 vs ValueError split as D41.
            capture_exception(error)
            return done(
                Render(), EmitFailure(error, lambda: self.select_phase(phase))
            )
        except ValueError as error:
            return done(Render(), EmitFailure(error))
        context.settings["selectedPhase"] = phase
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
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            self._ports.apply_outcome(done(LoadState()))
            self._ports.invalidate_task_selector()
            return done(Render(), EmitFailure(error))
        except ValueError as error:
            self._ports.apply_outcome(done(LoadState()))
            self._ports.invalidate_task_selector()
            return done(Render(), EmitFailure(error))
        return done(LoadState(), Render(), Synchronize())

    @staticmethod
    def _task_failure(
        change: TaskChange,
        error: Exception,
        retry: Callable[[], ControllerOutcome[None]],
    ) -> ControllerOutcome[None]:
        # The window words the failure from its type; the error text never
        # reaches the user.
        return done(EmitTaskFailure(change, error, retry))

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
            reject_blank_task_draft(title)
            task = task_from_title(title)
            if any(existing["id"] == task["id"] for existing in context.tasks):
                context.store.set_selected_task_id(task["id"])
            else:
                context.store.queue_task_operation_with_selection(
                    "upsert", task, task["id"])
            context.settings["selectedTaskId"] = task["id"]
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return self._task_failure(
                TaskChange.ADD, error, lambda: self.add_task(requested_title)
            )
        except ValueError as error:
            return self._task_failure(
                TaskChange.ADD, error, lambda: self.add_task(requested_title)
            )
        self._ports.clear_task_input()
        return done(LoadState(), Render(), Synchronize())

    def delete_task(self, task_id: str) -> ControllerOutcome[None]:
        self._task_delete_attempts += 1
        if self._ports.mutation_blocked():
            # H1: the user saw the refusal, so the delete ends here. Without
            # a window in view it stays recorded and is reported later.
            self._end_attended_task_delete(task_id)
            return done()
        context = self._context()
        task = context.known_tasks.get(task_id)
        if not task or self._recorded_in_another_workspace(task_id):
            self._settle_pending_task_delete(task_id)
            return done()
        try:
            if context.settings.get("selectedTaskId") == task_id:
                context.store.queue_task_operation_with_selection(
                    "delete", task, None)
                context.settings["selectedTaskId"] = None
            else:
                context.store.queue_task_operation("delete", task)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return self._task_delete_failure(task_id, error)
        except ValueError as error:
            return self._task_delete_failure(task_id, error)
        self._settle_pending_task_delete(task_id)
        return done(LoadState(), Render(), Synchronize())

    def _task_delete_failure(
        self, task_id: str, error: Exception
    ) -> ControllerOutcome[None]:
        # H1, M1: a failure the user sees ends the pending delete, so no
        # later action applies it silently. Only Retry keeps the task id.
        self._end_attended_task_delete(task_id)
        return self._task_failure(
            TaskChange.DELETE, error, lambda: self.delete_task(task_id)
        )

    def _end_attended_task_delete(self, task_id: str) -> None:
        if self._pending_task_delete_id != task_id:
            return
        if self._ports.task_delete_attended():
            self._end_pending_task_delete()

    def _end_pending_task_delete(self) -> ControllerOutcome[str | None]:
        """Forget the pending delete and its marker; return its task id.

        The outcome carries the store failure when the marker stays.
        """
        task_id = self._pending_task_delete_id
        if task_id is None:
            return returning(None)
        self._pending_task_delete_id = None
        self._withdrawn_task_delete_id = task_id
        failure = self._clear_withdrawn_marker()
        return returning(task_id) if failure is None else returning(task_id, failure)

    def retry_withdrawn_task_delete_clear(self) -> ControllerOutcome[None]:
        """Clear the marker of a delete that ended while its clear failed (M2)."""
        self._clear_withdrawn_marker()
        return done()

    def _clear_withdrawn_marker(self) -> EmitFailure | None:
        """Clear the withdrawn marker; return the failure when it stays."""
        if self._withdrawn_task_delete_id is None:
            return None
        try:
            clear_pending_task_deletion(self._context().store)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry. The id stays withdrawn
            # in this run; the next chance is the next retry point. Sentry
            # may be off, so the log keeps the error; at shutdown the next
            # start asks the user about the marker that is left.
            _LOGGER.warning(
                "The pending task delete marker could not be cleared: %r", error
            )
            capture_exception(error)
            return EmitFailure(error)
        self._withdrawn_task_delete_id = None
        return None

    def release_unfinished_task_delete(self) -> ControllerOutcome[str | None]:
        """End an unfinished delete before telling the user it did not happen.

        The returned task id lives on only in the notice's Retry action, so
        Cancel leaves nothing to apply later (H1). A marker the store could
        not clear comes back as a failure for the user to read; the next
        start asks about it again.
        """
        return self._end_pending_task_delete()

    def begin_pending_task_delete(self, task_id: str) -> ControllerOutcome[bool]:
        """Record a delete durably before its row hides for the undo window."""
        context = self._context()
        try:
            record_pending_task_deletion(context.store, task_id, context.user)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return returning(False, EmitFailure(error))
        self._pending_task_delete_id = task_id
        self._pending_task_delete_owner = workspace_owner_id(context.user)
        # The new marker replaced any withdrawn one.
        self._withdrawn_task_delete_id = None
        return returning(True)

    def withdraw_pending_task_delete(self) -> ControllerOutcome[None]:
        """Forget the recorded delete because the user pressed Undo."""
        task_id = self._pending_task_delete_id
        if task_id is None:
            return done()
        # Undo is final for this run even when the marker cannot be cleared.
        self._pending_task_delete_id = None
        self._withdrawn_task_delete_id = task_id
        return self._clear_withdrawn_task_delete()

    def _clear_withdrawn_task_delete(self) -> ControllerOutcome[None]:
        try:
            clear_pending_task_deletion(self._context().store)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            # M2: the withdrawn id keeps the clear due at shutdown.
            capture_exception(error)
            return done(EmitFailure(error, self._clear_withdrawn_task_delete))
        self._withdrawn_task_delete_id = None
        return done()

    def adopt_recorded_task_delete(self) -> ControllerOutcome[bool]:
        """Take over a delete an earlier run recorded; True when one awaits."""
        context = self._context()
        try:
            stored = stored_pending_task_deletion(context.store)
            if stored is None:
                return returning(False)
            marker = parse_pending_task_deletion(stored)
            if (
                marker is None
                or marker.owner_id != workspace_owner_id(context.user)
                or all(task["id"] != marker.task_id for task in context.tasks)
            ):
                # Fail closed: another workspace's marker, a missing task or
                # an unreadable marker deletes nothing.
                clear_pending_task_deletion(context.store)
                return returning(False)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return returning(False, EmitFailure(error))
        self._pending_task_delete_id = marker.task_id
        self._pending_task_delete_owner = marker.owner_id
        return returning(True)

    def complete_pending_task_delete_now(self) -> ControllerOutcome[None]:
        """Finish any pending delete before the workspace changes hands."""
        self._complete_pending_task_delete()
        # M2: a withdrawn delete must not count as queued work at sign-out.
        return self.retry_withdrawn_task_delete_clear()

    def _complete_pending_task_delete(self) -> bool:
        """Finish a pending delete first; False while the selected task awaits it."""
        attempts = self._task_delete_attempts
        self._ports.complete_pending_task_delete()
        task_id = self._pending_task_delete_id
        if task_id is None:
            return True
        if attempts == self._task_delete_attempts:
            # An earlier completion failed or was blocked; use the normal
            # delete path again before the task can be started.
            self._ports.apply_outcome(self.delete_task(task_id))
        if self._pending_task_delete_id is None:
            return True
        return self._context().settings.get("selectedTaskId") != task_id

    def _recorded_in_another_workspace(self, task_id: str) -> bool:
        """True when the pending delete belongs to a workspace that was replaced.

        Task ids derive from titles, so after a sign-out, an account switch or
        a reset the same id can name another owner's task. That delete is
        dropped, never applied to the new workspace (M1).
        """
        if task_id != self._pending_task_delete_id:
            return False
        return self._pending_task_delete_owner != workspace_owner_id(self._context().user)

    def _settle_pending_task_delete(self, task_id: str) -> None:
        """Clear the durable marker once its task is deleted or gone."""
        if self._pending_task_delete_id != task_id:
            return
        self._pending_task_delete_id = None
        try:
            clear_pending_task_deletion(self._context().store)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # The task is already deleted. A marker left behind names a
            # missing task, so the next start drops it.
            capture_exception(error)

    def rename_task(self, task_id: str, title: str) -> ControllerOutcome[None]:
        """Mirror core renameTask: upsert the new identity, then rename the old."""
        if self._ports.mutation_blocked():
            return done()
        context = self._context()
        old_task = context.known_tasks.get(task_id)
        if not old_task:
            return done()
        try:
            # B4: a rename follows the composer rule for a blank title.
            reject_blank_task_draft(title)
            new_task = task_from_title(title)
            if new_task["id"] == task_id:
                return done()
            was_selected = context.settings.get("selectedTaskId") == task_id
            self._queue_rename(context, old_task, new_task, was_selected)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            capture_exception(error)
            return self._task_failure(
                TaskChange.RENAME, error, lambda: self.rename_task(task_id, title)
            )
        except ValueError as error:
            return self._task_failure(
                TaskChange.RENAME, error, lambda: self.rename_task(task_id, title)
            )
        if was_selected:
            context.settings["selectedTaskId"] = new_task["id"]
        return done(LoadState(), Render(), Synchronize())

    @staticmethod
    def _queue_rename(
        context: TimerInteractionContext,
        old_task: dict[str, Any],
        new_task: dict[str, Any],
        was_selected: bool,
    ) -> None:
        store = context.store
        target_exists = any(
            isinstance(task, dict) and task.get("id") == new_task["id"]
            for task in context.tasks
        )
        if target_exists:
            # Renaming onto an existing task merges into it; only the
            # selection moves.
            if was_selected:
                store.set_selected_task_id(new_task["id"])
        elif was_selected:
            store.queue_task_operation_with_selection(
                "upsert", new_task, new_task["id"]
            )
        else:
            store.queue_task_operation("upsert", new_task)
        # Core 0.56.0: the rename carries finished history to the new identity.
        store.queue_task_rename(old_task, new_task)

    def duration_changed(self, phase: str, value: int) -> ControllerOutcome[None]:
        if self._ports.mutation_blocked():
            self._ports.refresh_duration_spins(self._context().settings)
            return done()
        context = self._context()
        try:
            context.store.queue_duration_operation(phase, value * 60_000)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            return done(
                LoadState(),
                EmitFailure(error, lambda: self.duration_changed(phase, value)),
            )
        except ValueError as error:
            return done(LoadState(), EmitFailure(error))
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
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D41: infra failure reports to Sentry, still notice-only for user.
            capture_exception(error)
            self._ports.apply_outcome(done(LoadState()))
            self._ports.refresh_auto_breaks(self._context().settings)
            return done(
                EmitFailure(error, lambda: self.auto_breaks_changed(enabled))
            )
        except ValueError as error:
            self._ports.apply_outcome(done(LoadState()))
            self._ports.refresh_auto_breaks(self._context().settings)
            return done(EmitFailure(error))
        return done(LoadState(), Synchronize())

    def schedule_pending_auto_break(
        self, *, require_canonical: bool | None = None
    ) -> ControllerOutcome[None]:
        has_pending, pending_notice = self._safe_has_pending_auto_break(
            self._context()
        )
        if pending_notice is not None:
            return done(pending_notice)
        if has_pending:
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
        # Apple parity: finished timers show Start, no Dismiss. The GUI
        # stop-sound control only silences the completion alert; the next
        # Start replaces the finished timer via queue_restart.
        self._ports.apply_outcome(self.stop_sound())
        return done()
