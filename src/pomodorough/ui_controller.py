from __future__ import annotations

import time  # public test seam: patches shared time module
from functools import wraps
from typing import Any

from PySide6.QtCore import QTimer  # noqa: F401 - public shared-class test seam

from .account_resolution_controller import (
    AccountPresentation,
    AccountResolutionContext,
    AccountResolutionController,
    AccountResolutionPorts,
)
from .controller_outcomes import (
    ActivatePersistedResolution,
    ControllerOutcome,
    EmitNotice,
    LoadState,
    MaybeAutoStartBreak,
    Render,
    RenderNetwork,
    SchedulePendingAutoBreak,
    SetAccountState,
    ShowStatus,
    ShowWindow,
    StopSound,
    Synchronize,
)
from .core import TERMINAL_STATUSES
from .replication_controller import (
    ReplicationContext,
    ReplicationController,
    ReplicationPorts,
)
from .shared_core import ProjectionApplyV2
from .synchronization_controller import (
    SynchronizationContext,
    SynchronizationController,
    SynchronizationPorts,
)
from .timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)


def presented_timer(
    projection: ProjectionApplyV2,
    snapshot: dict[str, Any],
    pending_commands: list[dict[str, Any]],
) -> dict[str, Any] | None:
    projected = projection.canonical_timer
    if projected is not None:
        return projected
    retained = snapshot.get("canonicalTimer")
    if (
        not isinstance(retained, dict)
        or retained.get("status") not in TERMINAL_STATUSES
    ):
        return None
    retained_id = retained.get("id")
    matching_history = any(
        item.get("timerId") == retained_id
        for item in snapshot.get("history", [])
        if isinstance(item, dict)
    )
    applied_clear = any(
        command.get("type") == "clear"
        and command.get("timerId") == retained_id
        and projection.timer_outcomes.get(command.get("id"), {}).get("outcome")
        == "applied"
        for command in pending_commands
    )
    return retained if matching_history and not applied_clear else None


class _OwnedState:
    def __init__(self, owner: str, field: str) -> None:
        self.owner = owner
        self.field = field

    def __get__(self, instance: Any, owner: type[Any]) -> Any:
        if instance is None:
            return self
        return getattr(instance._owned_controller(self.owner), self.field)

    def __set__(self, instance: Any, value: Any) -> None:
        setattr(instance._owned_controller(self.owner), self.field, value)


_CONTROLLER_METHODS: dict[str, tuple[str, str]] = {
    "_restore_replication": ("replication", "restore_replication"),
    "_replication_mode_changed": ("replication", "replication_mode_changed"),
    "_replication_transition_ready": (
        "replication",
        "replication_transition_ready",
    ),
    "_iroh_transition_ready": ("replication", "iroh_transition_ready"),
    "_activate_replication_mode": (
        "replication",
        "activate_replication_mode",
    ),
    "_create_iroh_room": ("replication", "create_iroh_room"),
    "_join_iroh_room": ("replication", "join_iroh_room"),
    "_leave_iroh_room": ("replication", "leave_iroh_room"),
    "_refresh_iroh_invite": ("replication", "refresh_iroh_invite"),
    "_sync_iroh_now": ("replication", "sync_iroh_now"),
    "_copy_iroh_invite": ("replication", "copy_iroh_invite"),
    "_iroh_status_changed": ("replication", "iroh_status_changed"),
    "_iroh_details_changed": ("replication", "iroh_details_changed"),
    "_iroh_invite_ready": ("replication", "iroh_invite_ready"),
    "_iroh_joined": ("replication", "iroh_joined"),
    "_iroh_projection_changed": ("replication", "iroh_projection_changed"),
    "_iroh_failure": ("replication", "iroh_failure"),
    "_selected_phase": ("timer", "selected_phase"),
    "_current_timer": ("timer", "current_timer"),
    "_tick": ("timer", "tick"),
    "_primary_action": ("timer", "primary_action"),
    "_issue": ("timer", "issue"),
    "_command_is_valid": ("timer", "command_is_valid"),
    "_queue_timer_command": ("timer", "queue_timer_command"),
    "_after_timer_command": ("timer", "after_timer_command"),
    "_maybe_auto_start_break": ("timer", "maybe_auto_start_break"),
    "_select_phase": ("timer", "select_phase"),
    "_task_selection_changed": ("timer", "task_selection_changed"),
    "_add_task": ("timer", "add_task"),
    "_delete_task": ("timer", "delete_task"),
    "_duration_changed": ("timer", "duration_changed"),
    "_auto_breaks_changed": ("timer", "auto_breaks_changed"),
    "_schedule_pending_auto_break": ("timer", "schedule_pending_auto_break"),
    "_stop_sound": ("timer", "stop_sound"),
    "_stop_sound_and_clear": ("timer", "stop_sound_and_clear"),
    "_sync": ("synchronization", "sync"),
    "_sync_when_available": ("synchronization", "sync_when_available"),
    "_retry_sync": ("synchronization", "retry_sync"),
    "_remote_revision_available": (
        "synchronization",
        "remote_revision_available",
    ),
    "_apply_sync": ("synchronization", "apply_sync"),
    "_cloud_failure": ("synchronization", "cloud_failure"),
    "_activate_persisted_resolution": (
        "account",
        "activate_persisted_resolution",
    ),
    "_schedule_resolution_retry": ("account", "schedule_resolution_retry"),
    "_retry_history_resolution": ("account", "retry_history_resolution"),
    "_resume_history_resolution": ("account", "resume_history_resolution"),
    "_continue_history_resolution": (
        "account",
        "continue_history_resolution",
    ),
    "_bootstrap_ready": ("account", "bootstrap_ready"),
    "_prompt_history_resolution": ("account", "prompt_history_resolution"),
    "_confirm_history_resolution": ("account", "confirm_history_resolution"),
    "_apply_resolution": ("account", "apply_resolution"),
    "_bootstrap_conflict": ("account", "bootstrap_conflict"),
    "_signed_in": ("account", "signed_in"),
    "_begin_history_resolution": ("account", "begin_history_resolution"),
    "_clear_history_resolution": ("account", "clear_history_resolution"),
    "_handle_resolution_corruption": (
        "account",
        "handle_resolution_corruption",
    ),
    "_quarantine_account_switch": ("account", "quarantine_account_switch"),
    "_accept_existing_account": ("account", "accept_existing_account"),
    "_signed_out": ("account", "signed_out"),
    "_session_expired": ("account", "session_expired"),
    "_set_account_state": ("account", "set_account_state"),
    "_delete_account_action": ("account", "delete_account_action"),
    "_account_deleted": ("account", "account_deleted"),
    "_account_deletion_failed": ("account", "account_deletion_failed"),
    "_account_action": ("account", "account_action"),
    "_choose_resolution_account_action": (
        "account",
        "choose_resolution_account_action",
    ),
    "_choose_account_switch_action": (
        "account",
        "choose_account_switch_action",
    ),
}

_SIMPLE_EFFECT_METHODS: dict[type[Any], str] = {
    LoadState: "_load_state",
    Render: "_render",
    RenderNetwork: "_render_network",
    Synchronize: "_sync",
    ShowWindow: "_show_window",
    ActivatePersistedResolution: "_activate_persisted_resolution",
    StopSound: "_stop_sound",
}


class ApplicationController:
    """Coordinates application projection and typed controller outcomes."""

    replication_mode = _OwnedState("replication", "mode")
    _iroh_status = _OwnedState("replication", "iroh_status")
    _iroh_details = _OwnedState("replication", "iroh_details")
    _iroh_invite = _OwnedState("replication", "iroh_invite")
    _cloud_restore_after_iroh_stop = _OwnedState(
        "replication", "cloud_restore_after_iroh_stop"
    )
    _iroh_join_pending = _OwnedState("replication", "iroh_join_pending")

    _notified_timer_id = _OwnedState("timer", "notified_timer_id")
    _sound_active = _OwnedState("timer", "sound_active")
    _alert_timer_identity = _OwnedState("timer", "alert_timer_identity")
    _auto_finish_in_progress = _OwnedState("timer", "auto_finish_in_progress")
    _auto_break_not_before = _OwnedState("timer", "auto_break_not_before")
    provisional_auto_break_timer_ids = _OwnedState(
        "timer", "provisional_auto_break_timer_ids"
    )

    _account_synced = _OwnedState("account", "account_synced")
    _history_resolution_active = _OwnedState("account", "history_resolution_active")
    _resolution_user = _OwnedState("account", "resolution_user")
    _resolution_phase = _OwnedState("account", "resolution_phase")
    _resolution_preview = _OwnedState("account", "resolution_preview")
    _resolution_request_id = _OwnedState("account", "resolution_request_id")
    _resolution_retry_paused = _OwnedState("account", "resolution_retry_paused")
    _resolution_retry_scheduled = _OwnedState("account", "resolution_retry_scheduled")
    _resolution_corruption = _OwnedState("account", "resolution_corruption")
    _account_switch_user = _OwnedState("account", "account_switch_user")

    _sync_request = _OwnedState("synchronization", "sync_request")
    _sync_waiting = _OwnedState("synchronization", "sync_waiting")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ensure_controllers()

    @property
    def timer_interactions(self) -> TimerInteractionController:
        return self._owned_controller("timer")

    @property
    def account_resolution(self) -> AccountResolutionController:
        return self._owned_controller("account")

    @property
    def synchronization(self) -> SynchronizationController:
        return self._owned_controller("synchronization")

    @property
    def replication(self) -> ReplicationController:
        return self._owned_controller("replication")

    def _owned_controller(self, name: str) -> Any:
        self._ensure_controllers()
        return self.__dict__[f"_{name}_controller"]

    def _ensure_controllers(self) -> None:
        if "_timer_controller" in self.__dict__:
            return
        self.__dict__["_timer_controller"] = self._build_timer_controller()
        self.__dict__["_account_controller"] = self._build_account_controller()
        self.__dict__["_synchronization_controller"] = (
            self._build_synchronization_controller()
        )
        self.__dict__["_replication_controller"] = self._build_replication_controller()

    def _build_timer_controller(self) -> TimerInteractionController:
        return TimerInteractionController(
            TimerInteractionPorts(
                context=self._timer_context,
                apply_outcome=self._apply_outcome,
                mutation_blocked=self._mutation_blocked,
                issue_command=lambda command, automatic: (
                    self._issue(command)
                    if automatic is None
                    else self._issue(command, automatic=automatic)
                ),
                maybe_auto_start_break=lambda **options: self._maybe_auto_start_break(
                    **options
                ),
                notice=lambda message: self.notice.emit(message),
                task_input_text=lambda: self.task_input.text(),
                clear_task_input=lambda: self.task_input.clear(),
                task_item_data=lambda index: self.task_combo.itemData(index),
                invalidate_task_selector=lambda: setattr(
                    self, "_task_selector_signature", None
                ),
                render_task_selector=lambda timer, active: self._render_task_selector(
                    timer, active
                ),
                refresh_duration_spins=lambda settings: (
                    self.timer_screen.refresh_duration_spins(settings)
                ),
                refresh_auto_breaks=lambda settings: (
                    self.timer_screen.refresh_auto_breaks(settings)
                ),
                stop_sound_timer=lambda: self.sound_timer.stop(),
                stop_completion_sound=lambda: self.completion_sound.stop(),
                set_stop_sound_control=self._set_stop_sound_control,
            )
        )

    def _build_account_controller(self) -> AccountResolutionController:
        return AccountResolutionController(
            AccountResolutionPorts(
                context=self._account_context,
                apply_outcome=self._apply_outcome,
                response_timing=self._response_timing,
                dialog_parent=lambda: self,
                present_account=self._present_account,
                prompt_history_resolution=lambda: self._prompt_history_resolution(),
                confirm_history_resolution=lambda strategy: (
                    self._confirm_history_resolution(strategy)
                ),
                choose_resolution_account_action=lambda: (
                    self._choose_resolution_account_action()
                ),
                choose_account_switch_action=lambda: (
                    self._choose_account_switch_action()
                ),
                continue_history_resolution=lambda: self._continue_history_resolution(),
                bootstrap_ready=lambda response: self._bootstrap_ready(response),
                signed_in=lambda user: self._signed_in(user),
                clear_sync_request=lambda: self.synchronization.clear_request(),
            )
        )

    def _build_synchronization_controller(self) -> SynchronizationController:
        return SynchronizationController(
            SynchronizationPorts(
                context=self._synchronization_context,
                apply_outcome=self._apply_outcome,
                response_timing=self._response_timing,
                activate_persisted_resolution=lambda: (
                    self._activate_persisted_resolution()
                ),
                continue_history_resolution=lambda: self._continue_history_resolution(),
                retry_sync=lambda: self._retry_sync(),
                synchronize=lambda: self._sync(),
                iroh_failure=lambda message: self._iroh_failure(message),
            )
        )

    def _build_replication_controller(self) -> ReplicationController:
        return ReplicationController(
            ReplicationPorts(
                context=self._replication_context,
                apply_outcome=self._apply_outcome,
                dialog_parent=lambda: self,
                replication_mode_data=lambda index: (
                    self.replication_mode_combo.itemData(index)
                ),
                set_replication_mode=lambda mode: (
                    self.network_screen.set_replication_mode(mode)
                ),
                show_screen=lambda index, sync: self._show_screen(index, sync=sync),
                focus_create_room=lambda reason: self.create_room_button.setFocus(
                    reason
                ),
                room_name_text=lambda: self.room_name_input.text(),
                invite_text=lambda: self.invite_input.toPlainText(),
                clear_invite_input=lambda: self.invite_input.clear(),
                iroh_failure=lambda message: self._iroh_failure(message),
            )
        )

    def __getattr__(self, name: str) -> Any:
        target = _CONTROLLER_METHODS.get(name)
        if target is None:
            raise AttributeError(name)
        controller_name, method_name = target
        method = getattr(self._owned_controller(controller_name), method_name)

        @wraps(method)
        def dispatch(*args: Any, **kwargs: Any) -> Any:
            result = method(*args, **kwargs)
            if isinstance(result, ControllerOutcome):
                self._apply_outcome(result)
                return result.value
            return result

        self.__dict__[name] = dispatch
        return dispatch

    def _apply_outcome(self, outcome: ControllerOutcome[Any]) -> None:
        for effect in outcome.effects:
            method_name = _SIMPLE_EFFECT_METHODS.get(type(effect))
            if method_name is not None:
                getattr(self, method_name)()
            else:
                self._apply_parameterized_effect(effect)

    def _apply_parameterized_effect(self, effect: Any) -> None:
        if isinstance(effect, SetAccountState):
            self._set_account_state(effect.synced)
        elif isinstance(effect, MaybeAutoStartBreak):
            self._maybe_auto_start_break(
                sync=effect.sync,
                allow_busy=effect.allow_busy,
                require_canonical=effect.require_canonical,
            )
        elif isinstance(effect, SchedulePendingAutoBreak):
            self._schedule_pending_auto_break(
                require_canonical=effect.require_canonical
            )
        elif isinstance(effect, EmitNotice):
            self.notice.emit(effect.message)
        elif isinstance(effect, ShowStatus):
            self.statusBar().showMessage(effect.message, effect.duration_ms)

    def _show_screen(self, index: int, *, sync: bool = True) -> None:
        self._display_screen(index)
        if sync and index and self.replication_mode == "centralized":
            self._sync()

    def _load_state(self) -> None:
        previous_timer = getattr(self, "timer", None)
        state, provisional_timer_ids, projection = self._project_current_state(
            previous_timer
        )
        self._read_resolution_corruption()
        self._install_projected_state(state, projection)
        self._reconcile_loaded_timer(previous_timer, provisional_timer_ids)
        self._install_projected_tasks(projection)
        self._refresh_loaded_controls()

    def _project_current_state(
        self, previous_timer: dict[str, Any] | None
    ) -> tuple[dict[str, Any], set[str], ProjectionApplyV2]:
        state, provisional_timer_ids = self.store.load_with_provisional_auto_breaks()
        physical_now_ms = int(time.time() * 1_000)
        projection_now_ms = self.store.effective_timer_now_ms(
            previous_timer,
            physical_ms=physical_now_ms,
        )
        self._projection_now_ms = projection_now_ms
        projection = self.store.projected_state(
            now_ms=projection_now_ms,
            state=state,
        )
        return state, provisional_timer_ids, projection

    def _read_resolution_corruption(self) -> None:
        try:
            self.store.pending_resolution()
            self.account_resolution.resolution_corruption = None
        except ValueError as error:
            self.account_resolution.resolution_corruption = str(error)

    def _install_projected_state(
        self, state: dict[str, Any], projection: ProjectionApplyV2
    ) -> None:
        self.settings = self.store.projected_settings(state, projection)
        self.revision = int(state["snapshot"].get("revision", 0))
        self.known_tasks = {
            task["id"]: task
            for task in state["snapshot"].get("knownTasks", [])
            if task.get("id") and task.get("title")
        }
        self.user = state["snapshot"].get("user")
        self.pending = state["pending"]
        self.pending_durations = state["pendingDurations"]
        self.pending_auto_starts = state["pendingAutoStarts"]
        self.timer = presented_timer(
            projection,
            state["snapshot"],
            state.get("projectionPending", state["pending"]),
        )
        self.history = self.store.projected_history(projection, state)

    def _reconcile_loaded_timer(
        self,
        previous_timer: dict[str, Any] | None,
        provisional_timer_ids: set[str],
    ) -> None:
        outcome = self.timer_interactions.reconcile_loaded_timer(
            previous_timer, provisional_timer_ids
        )
        self._apply_outcome(outcome)

    def _install_projected_tasks(self, projection: ProjectionApplyV2) -> None:
        self.tasks = projection.tasks
        for task in self.tasks:
            self.known_tasks[task["id"]] = task

    def _refresh_loaded_controls(self) -> None:
        if hasattr(self, "timer_screen"):
            self.timer_screen.refresh_duration_spins(self.settings)
            self.timer_screen.refresh_auto_breaks(self.settings)

    def _connect_cloud(self) -> None:
        self.cloud.signed_in.connect(self._signed_in)
        self.cloud.signed_out.connect(self._signed_out)
        self.cloud.session_expired.connect(self._session_expired)
        self.cloud.sync_ready.connect(self._apply_sync)
        self.cloud.bootstrap_ready.connect(self._bootstrap_ready)
        self.cloud.bootstrap_resolved.connect(self._apply_resolution)
        self.cloud.bootstrap_conflict.connect(self._bootstrap_conflict)
        self.cloud.revision_available.connect(self._remote_revision_available)
        self.cloud.authorization_stale.connect(self._sync)
        self.cloud.failure.connect(self._cloud_failure)
        self.cloud.account_deleted.connect(self._account_deleted)
        self.cloud.account_deletion_failed.connect(self._account_deletion_failed)

    def _connect_iroh(self) -> None:
        if self.iroh is None:
            return
        self.iroh.status_changed.connect(self._iroh_status_changed)
        self.iroh.details_changed.connect(self._iroh_details_changed)
        self.iroh.invite_ready.connect(self._iroh_invite_ready)
        self.iroh.joined.connect(self._iroh_joined)
        self.iroh.projection_changed.connect(self._iroh_projection_changed)
        self.iroh.failure.connect(self._iroh_failure)

    @staticmethod
    def _response_timing(response: dict[str, Any]) -> dict[str, int | None]:
        timing = getattr(response, "timing", None)
        if not isinstance(timing, dict):
            return {}
        return {
            "request_physical_ms": timing.get("requestPhysicalMs"),
            "received_physical_ms": timing.get("receivedPhysicalMs"),
            "request_monotonic_ms": timing.get("requestMonotonicMs"),
            "received_monotonic_ms": timing.get("receivedMonotonicMs"),
        }

    def _mutation_blocked(self) -> bool:
        if self.replication.iroh_join_pending:
            self.notice.emit(self.strings.text("network.wait_join"))
            return True
        if (
            not self.account_resolution.history_resolution_active
            and self.store.pending_resolution() is not None
        ):
            self._activate_persisted_resolution()
            self._render()
            self._set_account_state(False)
        if not self.account_resolution.history_resolution_active:
            return False
        self.notice.emit(self.strings.text("resolution.blocked"))
        return True

    def _timer_context(self) -> TimerInteractionContext:
        return TimerInteractionContext(
            store=self.store,
            cloud=self.cloud,
            closed=getattr(self, "_closed", False),
            timer=getattr(self, "timer", None),
            settings=getattr(self, "settings", {}),
            user=getattr(self, "user", None),
            tasks=getattr(self, "tasks", []),
            known_tasks=getattr(self, "known_tasks", {}),
            projection_now_ms=getattr(self, "_projection_now_ms", 0),
            replication_mode=self.replication.mode,
            history_resolution_active=(
                self.account_resolution.history_resolution_active
            ),
        )

    def _account_context(self) -> AccountResolutionContext:
        return AccountResolutionContext(
            store=self.store,
            cloud=self.cloud,
            strings=self.strings,
            user=self.user,
        )

    def _synchronization_context(self) -> SynchronizationContext:
        return SynchronizationContext(
            store=self.store,
            cloud=self.cloud,
            iroh=self.iroh,
            strings=self.strings,
            closed=self._closed,
            revision=self.revision,
            replication_mode=self.replication.mode,
            iroh_join_pending=self.replication.iroh_join_pending,
            history_resolution_active=(
                self.account_resolution.history_resolution_active
            ),
        )

    def _replication_context(self) -> ReplicationContext:
        return ReplicationContext(
            store=self.store,
            cloud=self.cloud,
            iroh=self.iroh,
            strings=self.strings,
            closed=self._closed,
        )

    def _present_account(self, presentation: AccountPresentation) -> None:
        button = self.account_button
        if button.property("authenticated") != presentation.authenticated:
            button.setProperty("authenticated", presentation.authenticated)
            button.style().unpolish(button)
            button.style().polish(button)
            button.updateGeometry()
        button.setText(presentation.text)
        button.setToolTip(presentation.tooltip)
        button.setAccessibleName(presentation.accessible_name)

    def _set_stop_sound_control(self, visible: bool) -> None:
        self.stop_sound_button.setVisible(visible)
        self.stop_sound_button.setEnabled(visible)


WindowApplicationController = ApplicationController
