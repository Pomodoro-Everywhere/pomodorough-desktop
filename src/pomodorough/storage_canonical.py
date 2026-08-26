from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Protocol

from .storage_canonical_acknowledgements import (
    CanonicalAcknowledgementStorage,
    validate_acknowledgements,
    validate_reconciliation_queues,
)
from .storage_canonical_installation import (
    AtomicCanonicalInstaller,
    validated_pending_resolution_apply,
)
from .storage_canonical_reconciliation import (
    SharedCoreReconciliationAdapter,
    core_canonical_timer,
    core_history,
    generated_break_day_bounds,
    reconciliation_output_fields,
    validated_reconciliation_dependencies,
    validated_reconciliation_id_sets,
)
from .storage_canonical_validation import (
    CanonicalWireValidator,
    require_sync_response_fields,
    valid_canonical_timer,
    valid_history_item,
    validated_sync_scalars,
    validated_sync_tasks,
)

__all__ = (
    "CanonicalResponseStorage",
    "CanonicalStorageDependencies",
    "valid_canonical_timer",
    "valid_history_item",
)


class _DisplayMinutes(Protocol):
    def __call__(self, duration_ms: int) -> int: ...


class _CanonicalDurations(Protocol):
    def __call__(self, durations_ms: Any) -> dict[str, int]: ...


class _DurationMs(Protocol):
    def __call__(self, value: Any, *, maximum: int = ...) -> int: ...


class _LogicalClock(Protocol):
    def __call__(
        self, value: Any, *, allow_legacy_zero: bool = False
    ) -> tuple[int, int]: ...


class _PhysicalTimeMs(Protocol):
    def __call__(self, value: Any) -> int: ...


class _NormalizeSettings(Protocol):
    def __call__(self, settings: Any) -> dict[str, Any]: ...


class _SetMeta(Protocol):
    def __call__(self, key: str, value: Any) -> None: ...


class _GetMeta(Protocol):
    def __call__(self, key: str, default: Any = None) -> Any: ...


class _ClockSampleForResponse(Protocol):
    def __call__(
        self,
        server_time_ms: int,
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None]: ...


class _PreflightPendingQueues(Protocol):
    def __call__(
        self, *, require_clock_coverage: bool = True
    ) -> dict[str, list[dict[str, Any]]]: ...


class _ProjectOperation(Protocol):
    def __call__(
        self,
        settings: dict[str, Any],
        *,
        duration_operation: dict[str, Any] | None = None,
        auto_start_operation: dict[str, Any] | None = None,
        selected_task_operation: dict[str, Any] | None = None,
        command_operation: dict[str, Any] | None = None,
        task_operation: dict[str, Any] | None = None,
        now: str | None = None,
        base: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        pending_commands: list[dict[str, Any]] | None = None,
    ) -> Any: ...


class _SetTrustedTimeAnchor(Protocol):
    def __call__(self, anchor: dict[str, int]) -> None: ...


class _PendingResolution(Protocol):
    def __call__(self, user_id: str | None = None) -> dict[str, Any] | None: ...


class _ValidatedProjectionState(Protocol):
    def __call__(
        self, projection: dict[str, Any], *, context: str
    ) -> tuple[
        dict[str, Any] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, int],
        bool,
        str | None,
    ]: ...


@dataclass(frozen=True)
class CanonicalStorageDependencies:
    connection: sqlite3.Connection
    device_id: str
    shared_core: Callable[[], Any]
    _canonical_durations: _CanonicalDurations
    _duration_ms: _DurationMs
    _logical_clock: _LogicalClock
    _physical_time_ms: _PhysicalTimeMs
    _normalize_settings: _NormalizeSettings
    _set_meta: _SetMeta
    get_meta: _GetMeta
    _clock_sample_for_response: _ClockSampleForResponse
    _display_minutes: _DisplayMinutes
    _ensure_no_pending_resolution: Callable[[], None]
    _immediate_transaction: Callable[[], Any]
    _preflight_pending_queues: _PreflightPendingQueues
    _project_operation: _ProjectOperation
    _prune_command_physical_times: Callable[[], None]
    _set_trusted_time_anchor: _SetTrustedTimeAnchor
    pending_resolution: _PendingResolution
    pending_sync: Callable[[], dict[str, Any] | None]
    _command_physical_times: Callable[[], dict[str, int]]
    _validated_projection_state: _ValidatedProjectionState


_COMPONENT_TYPES = {
    "_validation": CanonicalWireValidator,
    "_acknowledgements": CanonicalAcknowledgementStorage,
    "_reconciliation": SharedCoreReconciliationAdapter,
    "_installation": AtomicCanonicalInstaller,
}


def _component_method(
    component_name: str,
    method_name: str,
) -> Callable[..., Any]:
    component_method = getattr(_COMPONENT_TYPES[component_name], method_name)

    @wraps(component_method)
    def delegated(facade: Any, *args: Any, **kwargs: Any) -> Any:
        component = getattr(facade, component_name)
        return getattr(component, method_name)(*args, **kwargs)

    return delegated


class CanonicalResponseStorage:
    _validate_acknowledgements = staticmethod(validate_acknowledgements)
    _require_sync_response_fields = staticmethod(require_sync_response_fields)
    _validated_sync_scalars = staticmethod(validated_sync_scalars)
    _validated_sync_tasks = staticmethod(validated_sync_tasks)
    _generated_break_day_bounds = staticmethod(generated_break_day_bounds)
    _core_canonical_timer = staticmethod(core_canonical_timer)
    _core_history = staticmethod(core_history)
    _reconciliation_output_fields = staticmethod(reconciliation_output_fields)
    _validated_reconciliation_id_sets = staticmethod(validated_reconciliation_id_sets)
    _validate_reconciliation_queues = staticmethod(validate_reconciliation_queues)
    _validated_reconciliation_dependencies = staticmethod(
        validated_reconciliation_dependencies
    )
    _validated_pending_resolution_apply = staticmethod(
        validated_pending_resolution_apply
    )

    _validated_sync_response = _component_method(
        "_validation", "_validated_sync_response"
    )
    _validated_sync_server_clock = _component_method(
        "_validation", "_validated_sync_server_clock"
    )
    _validated_sync_history = _component_method(
        "_validation", "_validated_sync_history"
    )
    _valid_canonical_timer = _component_method("_validation", "_valid_canonical_timer")
    _valid_history_item = _component_method("_validation", "_valid_history_item")

    _validated_sync_acknowledgements = _component_method(
        "_acknowledgements", "_validated_sync_acknowledgements"
    )
    _reconcile_selected_phase_advances = _component_method(
        "_acknowledgements", "_reconcile_selected_phase_advances"
    )
    _reconcile_unmaterialized_auto_break_triggers = _component_method(
        "_acknowledgements", "_reconcile_unmaterialized_auto_break_triggers"
    )
    _apply_acknowledgements = _component_method(
        "_acknowledgements", "_apply_acknowledgements"
    )

    _core_timer_dependencies = _component_method(
        "_reconciliation", "_core_timer_dependencies"
    )
    _pending_generated_break_metadata = _component_method(
        "_reconciliation", "_pending_generated_break_metadata"
    )
    _core_timer_dependency = _component_method(
        "_reconciliation", "_core_timer_dependency"
    )
    _core_reconciliation_input = _component_method(
        "_reconciliation", "_core_reconciliation_input"
    )
    _core_canonical_response = _component_method(
        "_reconciliation", "_core_canonical_response"
    )
    _normalized_core_queue_operation = _component_method(
        "_reconciliation", "_normalized_core_queue_operation"
    )
    _validated_reconciliation_output = _component_method(
        "_reconciliation", "_validated_reconciliation_output"
    )
    _normalized_reconciliation_queues = _component_method(
        "_reconciliation", "_normalized_reconciliation_queues"
    )
    _validated_reconciliation_projection = _component_method(
        "_reconciliation", "_validated_reconciliation_projection"
    )
    _persist_core_reconciliation = _component_method(
        "_reconciliation", "_persist_core_reconciliation"
    )
    _persist_reconciliation_queue = _component_method(
        "_reconciliation", "_persist_reconciliation_queue"
    )
    _persist_reconciliation_dependencies = _component_method(
        "_reconciliation", "_persist_reconciliation_dependencies"
    )
    _reconcile_removed_auto_break_starts = _component_method(
        "_reconciliation", "_reconcile_removed_auto_break_starts"
    )
    _reconcile_with_shared_core = _component_method(
        "_reconciliation", "_reconcile_with_shared_core"
    )

    _delete_resolution_queue_ids = _component_method(
        "_installation", "_delete_resolution_queue_ids"
    )
    _install_canonical = _component_method("_installation", "_install_canonical")
    _clear_stale_timer_ownership = _component_method(
        "_installation", "_clear_stale_timer_ownership"
    )
    _merged_install_clock = _component_method("_installation", "_merged_install_clock")
    _install_projection = _component_method("_installation", "_install_projection")
    _install_projected_settings = _component_method(
        "_installation", "_install_projected_settings"
    )
    _install_snapshot = _component_method("_installation", "_install_snapshot")
    apply_sync = _component_method("_installation", "apply_sync")
    apply_resolution = _component_method("_installation", "apply_resolution")
    _response_clock_context = _component_method(
        "_installation", "_response_clock_context"
    )
    _prepare_resolution_reconciliation = _component_method(
        "_installation", "_prepare_resolution_reconciliation"
    )
    _clear_keep_remote_queues = _component_method(
        "_installation", "_clear_keep_remote_queues"
    )

    def __init__(self, dependencies: CanonicalStorageDependencies) -> None:
        self._dependencies = dependencies
        self._validation = CanonicalWireValidator(dependencies, self)
        self._acknowledgements = CanonicalAcknowledgementStorage(dependencies, self)
        self._reconciliation = SharedCoreReconciliationAdapter(dependencies, self)
        self._installation = AtomicCanonicalInstaller(dependencies, self)
