from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any, Protocol

from .storage_model import MAX_CLOCK_SKEW_MS, utc_timestamp

_RESOLUTION_QUEUE_DELETIONS = (
    ("commands", "DELETE FROM pending_commands WHERE id = ?"),
    (
        "taskOperations",
        "DELETE FROM pending_task_operations WHERE id = ?",
    ),
    (
        "durationOperations",
        "DELETE FROM pending_duration_operations WHERE id = ?",
    ),
    (
        "autoStartOperations",
        "DELETE FROM pending_auto_start_operations WHERE id = ?",
    ),
    (
        "selectedTaskOperations",
        "DELETE FROM pending_selected_task_operations WHERE id = ?",
    ),
)


class CanonicalInstallationDependencies(Protocol):
    connection: sqlite3.Connection

    def _clock_sample_for_response(
        self,
        server_time_ms: int,
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None]: ...

    def _display_minutes(self, duration_ms: int) -> int: ...

    def _ensure_no_pending_resolution(self) -> None: ...

    def _immediate_transaction(self) -> Any: ...

    def _logical_clock(
        self, value: Any, *, allow_legacy_zero: bool = False
    ) -> tuple[int, int]: ...

    def _normalize_settings(self, value: Any) -> dict[str, Any]: ...

    def _physical_time_ms(self, value: Any) -> int: ...

    def _preflight_pending_queues(
        self, *, require_clock_coverage: bool = True
    ) -> dict[str, list[dict[str, Any]]]: ...

    def _project_operation(self, *args: Any, **kwargs: Any) -> Any: ...

    def _prune_command_physical_times(self) -> None: ...

    def _set_meta(self, key: str, value: Any) -> None: ...

    def _set_trusted_time_anchor(self, anchor: dict[str, int]) -> None: ...

    def get_meta(self, key: str, default: Any = None) -> Any: ...

    def pending_resolution(
        self, user_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def pending_sync(self) -> dict[str, Any] | None: ...


class CanonicalInstallationHooks(Protocol):
    def _merged_install_clock(
        self,
        canonical: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
        trusted_response_ms: int,
    ) -> tuple[int, int]: ...

    def _clear_stale_timer_ownership(
        self,
        canonical: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None: ...

    def _install_projection(
        self,
        canonical: dict[str, Any],
        trusted_response_ms: int,
        expected: dict[str, Any] | None,
    ) -> Any: ...

    def _install_projected_settings(self, projection: Any) -> None: ...

    def _install_snapshot(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        preserve_known_tasks: bool,
    ) -> None: ...

    def _validated_sync_response(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]: ...

    def _response_clock_context(
        self,
        canonical: dict[str, Any],
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None, int]: ...

    def _reconcile_selected_phase_advances(
        self,
        canonical: dict[str, Any],
        discarded_command_ids: set[str] | None = None,
    ) -> None: ...

    def _reconcile_unmaterialized_auto_break_triggers(
        self,
        canonical: dict[str, Any],
        discarded_command_ids: set[str] | None = None,
    ) -> None: ...

    def _apply_acknowledgements(
        self,
        canonical: dict[str, Any],
        *,
        delete: bool = True,
    ) -> list[str]: ...

    def _reconcile_with_shared_core(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]: ...

    def _install_canonical(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        *,
        preserve_known_tasks: bool,
        clock_sample: dict[str, int] | None,
        trusted_response_ms: int,
        expected_projection: dict[str, Any] | None = None,
    ) -> None: ...

    def _validated_pending_resolution_apply(
        self,
        pending: dict[str, Any],
        request_id: str | None,
    ) -> tuple[dict[str, Any], str, dict[str, list[str]]]: ...

    def _prepare_resolution_reconciliation(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
        strategy: str,
        queue_ids: dict[str, list[str]],
    ) -> list[str]: ...

    def _clear_keep_remote_queues(self, queue_ids: dict[str, list[str]]) -> None: ...

    def _delete_resolution_queue_ids(self, queue_ids: dict[str, list[str]]) -> None: ...


def validated_pending_resolution_apply(
    pending: dict[str, Any],
    request_id: str | None,
) -> tuple[dict[str, Any], str, dict[str, list[str]]]:
    request = pending["request"]
    if request_id is not None and request.get("requestId") != request_id:
        raise ValueError("History resolution response matched a stale request.")
    strategy = request.get("strategy")
    if strategy not in {"keep_remote", "replace_remote", "merge"}:
        raise ValueError("Pending history resolution has an invalid strategy.")
    queue_ids = pending["queueIds"]
    request_ids = {
        key: [item.get("id") for item in request.get(key, [])] for key in queue_ids
    }
    if strategy == "keep_remote" and any(request_ids.values()):
        raise ValueError("Keep-remote resolution contains local operations.")
    if strategy != "keep_remote" and request_ids != queue_ids:
        raise ValueError(
            "Pending history resolution does not match captured queue IDs."
        )
    return request, strategy, queue_ids


class AtomicCanonicalInstaller:
    def __init__(
        self,
        store: CanonicalInstallationDependencies,
        hooks: CanonicalInstallationHooks,
    ) -> None:
        self._store = store
        self._hooks = hooks

    def _delete_resolution_queue_ids(self, queue_ids: dict[str, list[str]]) -> None:
        for key, statement in _RESOLUTION_QUEUE_DELETIONS:
            if key not in queue_ids:
                continue
            self._store.connection.executemany(
                statement, ((item_id,) for item_id in queue_ids[key])
            )

    def _install_canonical(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        *,
        preserve_known_tasks: bool,
        clock_sample: dict[str, int] | None,
        trusted_response_ms: int,
        expected_projection: dict[str, Any] | None = None,
    ) -> None:
        pending = self._store._preflight_pending_queues(require_clock_coverage=False)
        merged_clock = self._hooks._merged_install_clock(
            canonical, pending, trusted_response_ms
        )
        self._hooks._clear_stale_timer_ownership(canonical, pending)
        projection = self._hooks._install_projection(
            canonical, trusted_response_ms, expected_projection
        )
        self._hooks._install_projected_settings(projection)
        self._hooks._install_snapshot(canonical, user, preserve_known_tasks)
        self._store._set_meta("autoStartLegacyDefaultUnknown", False)
        self._store._set_meta(
            "hlc", {"wallMs": merged_clock[0], "counter": merged_clock[1]}
        )
        if clock_sample is not None:
            self._store._set_meta("serverClockSample", clock_sample)

    def _clear_stale_timer_ownership(
        self,
        canonical: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None:
        ownership = self._store.get_meta("centralizedTimerOwnership")
        owned_timer_id = (
            ownership.get("timerId") if isinstance(ownership, dict) else None
        )
        canonical_timer = canonical["canonicalTimer"]
        retained_timer_ids = {
            operation.get("timerId")
            for operation in pending["commands"]
            if operation.get("type") == "start"
        }
        if (
            owned_timer_id is not None
            and (
                not isinstance(canonical_timer, dict)
                or canonical_timer.get("id") != owned_timer_id
            )
            and owned_timer_id not in retained_timer_ids
        ):
            self._store._set_meta("centralizedTimerOwnership", None)

    def _merged_install_clock(
        self,
        canonical: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
        trusted_response_ms: int,
    ) -> tuple[int, int]:
        now_ms = self._store._physical_time_ms(trusted_response_ms)
        local_clock = self._store._logical_clock(
            self._store.get_meta("hlc", {"wallMs": 0, "counter": 0}),
            allow_legacy_zero=True,
        )
        server_clock = (
            canonical["serverHlcWallMs"],
            canonical["serverHlcCounter"],
        )
        retained_clocks = [
            (operation["hlcWallMs"], operation["hlcCounter"])
            for operations in (
                pending["commands"],
                pending["taskOperations"],
                pending["durationOperations"],
                pending["autoStartOperations"],
                pending["selectedTaskOperations"],
            )
            for operation in operations
            if operation["hlcWallMs"] > 0
        ]
        retained_clock = max(retained_clocks, default=(0, 0))
        if retained_clock[0] - now_ms > MAX_CLOCK_SKEW_MS:
            raise ValueError(
                "Retained pending operation exceeds the trusted-time limit."
            )
        candidates = [server_clock, retained_clock]
        local_wall = local_clock[0]
        if local_wall > 0 and local_wall - now_ms <= MAX_CLOCK_SKEW_MS:
            candidates.append(local_clock)
        return max(candidates)

    def _install_projection(
        self,
        canonical: dict[str, Any],
        trusted_response_ms: int,
        expected: dict[str, Any] | None,
    ) -> Any:
        settings = self._store._normalize_settings(self._store.get_meta("settings", {}))
        projection_settings = deepcopy(settings)
        projection_settings["durationsMs"] = canonical["durationsMs"]
        projection = self._store._project_operation(
            projection_settings,
            now=utc_timestamp(trusted_response_ms),
            base=canonical,
        )
        actual = {
            "canonicalTimer": projection.canonical_timer,
            "history": projection.history,
            "tasks": projection.tasks,
            "durationsMs": projection.durations_ms,
            "autoStartBreaks": projection.auto_start_breaks,
            "selectedTaskId": projection.selected_task_id,
        }
        if expected is not None and actual != expected:
            raise ValueError(
                "Shared core reconciliation disagreed with projection replay."
            )
        return projection

    def _install_projected_settings(self, projection: Any) -> None:
        settings = self._store._normalize_settings(self._store.get_meta("settings", {}))
        projected_durations = projection.durations_ms
        settings["durationsMs"] = projected_durations
        settings["durations"] = {
            phase: self._store._display_minutes(duration_ms)
            for phase, duration_ms in settings["durationsMs"].items()
        }
        settings["autoStartBreaks"] = projection.auto_start_breaks
        settings["selectedTaskId"] = projection.selected_task_id
        self._store._set_meta("settings", settings)

    def _install_snapshot(
        self,
        canonical: dict[str, Any],
        user: dict[str, Any] | None,
        preserve_known_tasks: bool,
    ) -> None:
        previous = self._store.get_meta("snapshot", {})
        known = (
            {
                task["id"]: task
                for task in previous.get("knownTasks", [])
                if task.get("id") and task.get("title")
            }
            if preserve_known_tasks
            else {}
        )
        for task in canonical["tasks"]:
            if task.get("id") and task.get("title"):
                known[task["id"]] = task
        self._store._set_meta(
            "snapshot",
            {
                "revision": canonical["revision"],
                "canonicalTimer": canonical["canonicalTimer"],
                "history": canonical["history"],
                "tasks": canonical["tasks"],
                "knownTasks": sorted(
                    known.values(),
                    key=lambda item: (item["title"].casefold(), item["id"]),
                ),
                "autoStartBreaks": canonical["autoStartBreaks"],
                "selectedTaskId": canonical["selectedTaskId"],
                "user": user,
            },
        )

    def apply_sync(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        with self._store._immediate_transaction():
            self._store._ensure_no_pending_resolution()
            claimed = self._store.pending_sync()
            if claimed != request:
                raise ValueError(
                    "Sync response did not match an active normal sync claim."
                )
            previous = self._store.get_meta("snapshot")
            canonical = self._hooks._validated_sync_response(response, request)
            clock_sample, clock_anchor, trusted_response_ms = (
                self._hooks._response_clock_context(
                    canonical,
                    request_physical_ms,
                    received_physical_ms,
                    request_monotonic_ms,
                    received_monotonic_ms,
                )
            )
            notices = self._finish_sync_install(
                canonical, request, previous, clock_sample, trusted_response_ms
            )
            if claimed is not None:
                self._store._set_meta("pendingSync", None)
        if clock_anchor is not None:
            self._store._set_trusted_time_anchor(clock_anchor)
        return notices

    def _finish_sync_install(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
        previous: dict[str, Any],
        clock_sample: dict[str, int] | None,
        trusted_response_ms: int,
    ) -> list[str]:
        if canonical["revision"] < int(previous.get("revision", 0)):
            raise ValueError("Server response would regress canonical revision.")
        self._hooks._reconcile_selected_phase_advances(canonical)
        self._hooks._reconcile_unmaterialized_auto_break_triggers(canonical)
        notices = self._hooks._apply_acknowledgements(canonical, delete=False)
        reconciliation = self._hooks._reconcile_with_shared_core(canonical, request)
        self._hooks._install_canonical(
            canonical,
            previous.get("user"),
            preserve_known_tasks=True,
            clock_sample=clock_sample,
            trusted_response_ms=trusted_response_ms,
            expected_projection=reconciliation["projection"],
        )
        self._store._prune_command_physical_times()
        return notices

    def apply_resolution(
        self,
        response: dict[str, Any],
        user: dict[str, Any],
        request_id: str | None = None,
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        user_id = user.get("id") if isinstance(user, dict) else None
        with self._store._immediate_transaction():
            pending = self._store.pending_resolution(user_id)
            if pending is None:
                raise ValueError("No matching history resolution is pending.")
            request, strategy, queue_ids = (
                self._hooks._validated_pending_resolution_apply(pending, request_id)
            )
            canonical = self._hooks._validated_sync_response(response, request)
            clock_sample, clock_anchor, trusted_response_ms = (
                self._hooks._response_clock_context(
                    canonical,
                    request_physical_ms,
                    received_physical_ms,
                    request_monotonic_ms,
                    received_monotonic_ms,
                )
            )
            notices = self._finish_resolution_install(
                canonical,
                request,
                strategy,
                queue_ids,
                user,
                clock_sample,
                trusted_response_ms,
            )
            self._store._set_meta("pendingResolution", None)
        if clock_anchor is not None:
            self._store._set_trusted_time_anchor(clock_anchor)
        return notices

    def _finish_resolution_install(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
        strategy: str,
        queue_ids: dict[str, list[str]],
        user: dict[str, Any],
        clock_sample: dict[str, int] | None,
        trusted_response_ms: int,
    ) -> list[str]:
        notices = self._hooks._prepare_resolution_reconciliation(
            canonical, request, strategy, queue_ids
        )
        reconciliation = self._hooks._reconcile_with_shared_core(canonical, request)
        self._hooks._install_canonical(
            canonical,
            user,
            preserve_known_tasks=strategy != "keep_remote",
            clock_sample=clock_sample,
            trusted_response_ms=trusted_response_ms,
            expected_projection=reconciliation["projection"],
        )
        self._store._prune_command_physical_times()
        return notices

    def _response_clock_context(
        self,
        canonical: dict[str, Any],
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None, int]:
        sample, anchor = self._store._clock_sample_for_response(
            canonical["serverTimeMs"],
            request_physical_ms,
            received_physical_ms,
            request_monotonic_ms,
            received_monotonic_ms,
        )
        trusted_response_ms = (
            canonical["serverTimeMs"] if anchor is None else anchor["acquiredTrustedMs"]
        )
        return sample, anchor, trusted_response_ms

    def _prepare_resolution_reconciliation(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
        strategy: str,
        queue_ids: dict[str, list[str]],
    ) -> list[str]:
        previous = self._store.get_meta("snapshot", {})
        minimum_revision = max(
            int(previous.get("revision", 0)), int(request["expectedRevision"])
        )
        if canonical["revision"] < minimum_revision:
            raise ValueError("Server response would regress canonical revision.")
        discarded = set(queue_ids["commands"]) if strategy == "keep_remote" else None
        self._hooks._reconcile_selected_phase_advances(canonical, discarded)
        self._hooks._reconcile_unmaterialized_auto_break_triggers(canonical, discarded)
        notices = self._hooks._apply_acknowledgements(canonical, delete=False)
        if strategy == "keep_remote":
            self._hooks._clear_keep_remote_queues(queue_ids)
        return notices

    def _clear_keep_remote_queues(self, queue_ids: dict[str, list[str]]) -> None:
        self._hooks._delete_resolution_queue_ids(queue_ids)
        if "autoStartOperations" not in queue_ids:
            self._store.connection.execute("DELETE FROM pending_auto_start_operations")
        if "selectedTaskOperations" not in queue_ids:
            self._store.connection.execute(
                "DELETE FROM pending_selected_task_operations"
            )
        self._store.connection.execute("DELETE FROM pending_auto_breaks")
        self._store.connection.execute("DELETE FROM pending_auto_break_starts")
