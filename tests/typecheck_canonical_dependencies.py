from __future__ import annotations

from pomodorough.storage import Store
from pomodorough.storage_canonical import CanonicalStorageDependencies


def canonical_dependencies_from_store(store: Store) -> CanonicalStorageDependencies:
    """Type-check the production Store-to-canonical dependency boundary."""
    return CanonicalStorageDependencies(
        connection=store.connection,
        device_id=store.device_id,
        shared_core=lambda: store._shared_core,
        _canonical_durations=store._canonical_durations,
        _duration_ms=store._duration_ms,
        _logical_clock=store._logical_clock,
        _physical_time_ms=store._physical_time_ms,
        _normalize_settings=store._normalize_settings,
        _set_meta=store._set_meta,
        get_meta=store.get_meta,
        _clock_sample_for_response=lambda server_time_ms,
        request_physical_ms,
        received_physical_ms,
        request_monotonic_ms,
        received_monotonic_ms: store._clock_sample_for_response(
            server_time_ms,
            request_physical_ms,
            received_physical_ms,
            request_monotonic_ms,
            received_monotonic_ms,
        ),
        _display_minutes=store._display_minutes,
        _ensure_no_pending_resolution=store._sync_storage._ensure_no_pending_resolution,
        _immediate_transaction=store._immediate_transaction,
        _preflight_pending_queues=store._preflight_pending_queues,
        _project_operation=store._project_operation,
        _prune_command_physical_times=store._prune_command_physical_times,
        _set_trusted_time_anchor=lambda anchor: store._set_trusted_time_anchor(anchor),
        pending_resolution=store._sync_storage.pending_resolution,
        pending_sync=store._sync_storage.pending_sync,
        _command_physical_times=store._command_physical_times,
        _validated_projection_state=store._validated_projection_state,
    )
