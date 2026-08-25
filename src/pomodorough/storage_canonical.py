from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, Protocol

from .storage_canonical_acknowledgements import (
    CanonicalAcknowledgementDependencies,
    CanonicalAcknowledgementStorage,
    validate_acknowledgements,
    validate_reconciliation_queues,
)
from .storage_canonical_installation import (
    AtomicCanonicalInstaller,
    CanonicalInstallationDependencies,
    validated_pending_resolution_apply,
)
from .storage_canonical_reconciliation import (
    CanonicalReconciliationDependencies,
    SharedCoreReconciliationAdapter,
    core_canonical_timer,
    core_history,
    generated_break_day_bounds,
    reconciliation_output_fields,
    validated_reconciliation_dependencies,
    validated_reconciliation_id_sets,
)
from .storage_canonical_validation import (
    CanonicalValidationDependencies,
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


class CanonicalStorageDependencies(
    CanonicalValidationDependencies,
    CanonicalAcknowledgementDependencies,
    CanonicalReconciliationDependencies,
    CanonicalInstallationDependencies,
    Protocol,
):
    pass


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

    def __init__(self, store: CanonicalStorageDependencies) -> None:
        self._store = store
        self._validation = CanonicalWireValidator(store, self)
        self._acknowledgements = CanonicalAcknowledgementStorage(store, self)
        self._reconciliation = SharedCoreReconciliationAdapter(store, self)
        self._installation = AtomicCanonicalInstaller(store, self)
