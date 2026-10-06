from __future__ import annotations

import json
import sqlite3
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from .core import PHASES
from .shared_core import SharedCoreOperationError
from .storage_model import (
    MAX_SAFE_INTEGER,
    _default_shared_core,
)

_SYNC_DOMAINS = (
    "commands",
    "taskOperations",
    "durationOperations",
    "autoStartOperations",
    "selectedTaskOperations",
)
_SYNC_LIMITS = {"perDomain": 256, "total": 512}
_BOOTSTRAP_LIMITS = {"perDomain": 4096, "total": 8192}
_BATCH_CURSOR_KEY = "batchNextDomain"
_STRATEGY_BATCH_MODES = {
    "keep_remote": "keep_remote",
    "replace_remote": "replace_remote",
    "merge": "merge",
}
_BATCH_PLAN_STATUSES = {
    "planned",
    "blocked_dependency",
    "oversized",
    "replay_saved",
    "oversized_saved",
}


def _batch_descriptors(
    pending: dict[str, Any], device_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Scheduling descriptors for the Core planner, never wire payloads."""
    descriptors = {}
    for domain in _SYNC_DOMAINS:
        items = []
        for operation in pending[domain]:
            item = {
                "id": operation["id"],
                "deviceId": operation.get("deviceId") or device_id,
                "hlcWallMs": operation["hlcWallMs"],
                "hlcCounter": operation["hlcCounter"],
            }
            if domain == "commands":
                item["deviceSequence"] = operation["deviceSequence"]
            items.append(item)
        descriptors[domain] = items
    return descriptors


def _batch_timer_dependencies(connection: Any) -> list[dict[str, str]]:
    """Complete timer edges; the planner derives the prefix barrier."""
    rows = connection.execute(
        "SELECT id, depends_on_command_id FROM pending_commands "
        "WHERE depends_on_command_id IS NOT NULL ORDER BY device_sequence"
    )
    return [
        {
            "operationId": str(row["id"]),
            "dependsOnOperationId": str(row["depends_on_command_id"]),
        }
        for row in rows
    ]


def _planner_unsupported(error: SharedCoreOperationError) -> bool:
    return (
        error.operation == "sync.batchPlan.v1" and "unsupported" in error.detail
    )


def _validated_batch_plan(value: object) -> dict[str, Any]:
    invalid = ValueError("Shared core returned an invalid batch plan.")
    if not isinstance(value, dict):
        raise invalid
    if value.get("status") not in _BATCH_PLAN_STATUSES:
        raise invalid
    selected = value.get("selected")
    if not isinstance(selected, dict) or set(selected) != set(_SYNC_DOMAINS):
        raise invalid
    for ids in selected.values():
        if (
            not isinstance(ids, list)
            or any(not isinstance(item, str) or not item for item in ids)
        ):
            raise invalid
    if value.get("nextDomain") is not None and value.get("nextDomain") not in _SYNC_DOMAINS:
        raise invalid
    return value

_PENDING_RESOLUTION_QUEUE_DOMAINS = (
    frozenset({"commands", "taskOperations", "durationOperations"}),
    frozenset(
        {
            "commands",
            "taskOperations",
            "durationOperations",
            "autoStartOperations",
        }
    ),
    frozenset(
        {
            "commands",
            "taskOperations",
            "durationOperations",
            "autoStartOperations",
            "selectedTaskOperations",
        }
    ),
)

@dataclass(frozen=True)
class SyncStorageDependencies:
    connection: sqlite3.Connection
    device_id: Callable[[], str]
    shared_core: Callable[[], Any]
    validate_integer: Callable[..., int]
    response_clock_sample: Callable[..., tuple[Any, Any]]
    transaction: Callable[[], Any]
    preflight_pending_queues: Callable[..., dict[str, Any]]
    project_operation: Callable[..., Any]
    write_meta: Callable[[str, Any], None]
    set_trusted_time_anchor: Callable[[dict[str, int]], None]
    validate_sync_response: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    read_meta: Callable[..., Any]
    load_state: Callable[..., dict[str, Any]]
    replace_meta: Callable[[str, Any], None]
    retire_delivery_proof: Callable[[dict[str, list[str]]], None]


class SyncStorage:
    def __init__(self, dependencies: SyncStorageDependencies) -> None:
        self._dependencies = dependencies

    @staticmethod
    def _wire_preference_operations(
        operations: Any, error_message: str
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or any(
            not isinstance(operation, dict) for operation in operations
        ):
            raise ValueError(error_message)
        outbound = []
        for operation in operations:
            item = dict(operation)
            item.pop("deviceId", None)
            outbound.append(item)
        return outbound

    def _replace_meta_inside_or_outside_transaction(self, key: str, value: Any) -> None:
        if self._dependencies.connection.in_transaction:
            self._dependencies.write_meta(key, value)
        else:
            self._dependencies.replace_meta(key, value)

    def _try_batch_plan(
        self, batch_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Core plan, or None when the bundle predates the operation.

        Compat gate for the Core upgrade: legacy per-domain slicing stays
        until the pinned bundle serves sync.batchPlan.v1. No client-side
        budgeting is invented here; remove the legacy branch with the repin.
        """
        core = self._dependencies.shared_core() or _default_shared_core()
        try:
            value = core.dispatch("sync.batchPlan.v1", batch_input)
        except SharedCoreOperationError as error:
            if _planner_unsupported(error):
                return None
            # D68: only operation rejection is validation; load/ABI stay infra.
            raise ValueError(str(error)) from error
        return _validated_batch_plan(value)

    def _batch_cursor(self) -> str:
        cursor = self._dependencies.read_meta(_BATCH_CURSOR_KEY, "commands")
        return cursor if cursor in _SYNC_DOMAINS else "commands"

    def _plan_new_sync(
        self, pending: dict[str, Any], device_id: str
    ) -> dict[str, Any] | None:
        return self._try_batch_plan(
            {
                "kind": "new",
                "mode": "sync",
                "limits": dict(_SYNC_LIMITS),
                "nextDomain": self._batch_cursor(),
                "queues": _batch_descriptors(pending, device_id),
                "timerDependencies": _batch_timer_dependencies(
                    self._dependencies.connection
                ),
            }
        )

    def _selected_sync_records(
        self, pending: dict[str, Any], plan: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        indexed = {
            domain: {item["id"]: item for item in pending[domain]}
            for domain in _SYNC_DOMAINS
        }
        try:
            resolved = {
                domain: [indexed[domain][item_id] for item_id in plan["selected"][domain]]
                for domain in _SYNC_DOMAINS
            }
        except KeyError as error:
            raise ValueError("Shared core selected an unknown operation.") from error
        return {
            "commands": resolved["commands"],
            "taskOperations": resolved["taskOperations"],
            "durationOperations": resolved["durationOperations"],
            "autoStartOperations": self._wire_preference_operations(
                resolved["autoStartOperations"],
                "Pending auto-start operation is corrupted.",
            ),
            "selectedTaskOperations": self._wire_preference_operations(
                resolved["selectedTaskOperations"],
                "Pending selected-task operation is corrupted.",
            ),
        }

    def _legacy_sync_payload(
        self, pending: dict[str, Any], device_id: str, revision: int
    ) -> dict[str, Any]:
        """Pre-Core per-domain slicing; remove with the repin (R43-D02 compat)."""
        payload = {
            "deviceId": device_id,
            "lastRevision": revision,
            "commands": pending["sendableCommands"][:256],
            "taskOperations": pending["taskOperations"][:256],
            "durationOperations": pending["durationOperations"][:256],
            "autoStartOperations": self._wire_preference_operations(
                pending["autoStartOperations"][:256],
                "Pending auto-start operation is corrupted.",
            ),
            "selectedTaskOperations": self._wire_preference_operations(
                pending["selectedTaskOperations"][:256],
                "Pending selected-task operation is corrupted.",
            ),
        }
        self._dependencies.write_meta("pendingSync", payload)
        self._dependencies.retire_delivery_proof(
            {key: [item["id"] for item in payload[key]]
             for key in ("commands", "taskOperations", "durationOperations",
                         "autoStartOperations", "selectedTaskOperations")}
        )
        return payload

    def sync_payload(self) -> dict[str, Any]:
        with self._dependencies.transaction():
            self._ensure_no_pending_resolution()
            claimed = self.pending_sync()
            if claimed is not None:
                return claimed
            pending = self._dependencies.preflight_pending_queues()
            device_id = self._dependencies.device_id()
            snapshot = self._dependencies.read_meta("snapshot", {})
            revision = self._dependencies.validate_integer(
                snapshot.get("revision", 0), "Persisted revision"
            )
            plan = self._plan_new_sync(pending, device_id)
            if plan is None:
                return self._legacy_sync_payload(pending, device_id, revision)
            selected = self._selected_sync_records(pending, plan)
            payload = {
                "deviceId": device_id,
                "lastRevision": revision,
                **selected,
            }
            if plan["status"] != "planned":
                # Timer barrier with no selectable operation: pull without
                # persisting a claim, cursor, or proof change.
                return payload
            self._dependencies.write_meta("pendingSync", payload)
            self._dependencies.write_meta(_BATCH_CURSOR_KEY, plan["nextDomain"])
            self._dependencies.retire_delivery_proof(
                {key: [item["id"] for item in selected[key]] for key in _SYNC_DOMAINS}
            )
        return payload

    def _saved_claim_status(
        self, claim: dict[str, Any], mode: str, limits: dict[str, int]
    ) -> str:
        queues = {}
        for domain in _SYNC_DOMAINS:
            ids = []
            for item in claim.get(domain, []):
                ids.append(item.get("id") if isinstance(item, dict) else item)
            queues[domain] = ids
        try:
            plan = self._try_batch_plan(
                {
                    "kind": "saved",
                    "mode": mode,
                    "limits": dict(limits),
                    "queues": queues,
                }
            )
        except ValueError:
            return "invalid"
        if plan is None:
            return "unsupported"
        return str(plan["status"])

    def _check_saved_claim(
        self, claim: dict[str, Any], mode: str, limits: dict[str, int], label: str
    ) -> None:
        status = self._saved_claim_status(claim, mode, limits)
        if status in ("replay_saved", "unsupported"):
            # Unsupported bundles cannot validate the claim; legacy accept.
            return
        if status == "oversized_saved":
            raise ValueError(
                f"{label} exceeds Core aggregate limits and cannot be sent. "
                "The original request is retained and nothing was sent. Queued "
                "work is intact; recover with discard_saved_sync_claim(confirmed=True)."
            )
        raise ValueError(f"{label} is corrupted.")

    def discard_saved_sync_claim(self, *, confirmed: bool = False) -> bool:
        """Drop an unrecoverable saved sync claim after explicit confirmation.

        The claim may have been delivered, so its operation IDs stay queued
        for exact-ID retry; only the saved request bytes are cleared. A
        fitting claim is never discarded here: it must replay exactly.
        """
        with self._dependencies.transaction():
            claim = self._dependencies.read_meta("pendingSync")
            if claim is None:
                return False
            status = self._saved_claim_status(claim, "sync", _SYNC_LIMITS)
            if status == "replay_saved":
                raise ValueError("Saved sync request is valid and must replay exactly.")
            if not confirmed:
                raise ValueError(
                    "Discarding the saved sync request requires explicit confirmation."
                )
            self._dependencies.write_meta("pendingSync", None)
            return True

    def _shaped_pending_sync(self, pending: Any) -> dict[str, Any] | None:
        """Shape validation and wire normalization for the saved sync claim."""
        if pending is None:
            return None
        current_keys = {
            "deviceId",
            "lastRevision",
            "commands",
            "taskOperations",
            "durationOperations",
            "autoStartOperations",
            "selectedTaskOperations",
        }
        legacy_keys = current_keys - {"selectedTaskOperations"}
        if isinstance(pending, dict) and set(pending) == legacy_keys:
            pending = {**pending, "selectedTaskOperations": []}
        if (
            not isinstance(pending, dict)
            or set(pending) != current_keys
            or pending.get("deviceId") != self._dependencies.device_id()
            or any(
                not isinstance(pending.get(key), list)
                for key in (
                    "commands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                    "selectedTaskOperations",
                )
            )
        ):
            raise ValueError("Pending normal sync claim is corrupted.")
        self._dependencies.validate_integer(
            pending.get("lastRevision"), "Pending normal sync revision"
        )
        return {
            **pending,
            "autoStartOperations": self._wire_preference_operations(
                pending["autoStartOperations"],
                "Pending normal sync claim is corrupted.",
            ),
            "selectedTaskOperations": self._wire_preference_operations(
                pending["selectedTaskOperations"],
                "Pending normal sync claim is corrupted.",
            ),
        }

    def pending_sync(self) -> dict[str, Any] | None:
        pending = self._dependencies.read_meta("pendingSync")
        original = deepcopy(pending)
        shaped = self._shaped_pending_sync(pending)
        if shaped is None:
            return None
        self._check_saved_claim(
            shaped, "sync", _SYNC_LIMITS, "Pending normal sync claim"
        )
        if shaped != original:
            self._replace_meta_inside_or_outside_transaction("pendingSync", shaped)
        return shaped

    def has_sendable_sync_operations(self) -> bool:
        with self._dependencies.transaction():
            self._ensure_no_pending_resolution()
            pending = self._dependencies.preflight_pending_queues()
            return any(
                pending[key]
                for key in (
                    "sendableCommands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                    "selectedTaskOperations",
                )
            )

    def pending_resolution(self, user_id: str | None = None) -> dict[str, Any] | None:
        try:
            pending = self._dependencies.read_meta("pendingResolution")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Pending account history is corrupted.") from error
        if pending is None:
            return None
        owner, request, _queue_ids = self._validated_pending_resolution(pending)
        normalized_request = self._normalized_pending_resolution_request(request)
        if normalized_request != request:
            pending = {**pending, "request": normalized_request}
            self._replace_meta_inside_or_outside_transaction(
                "pendingResolution", pending
            )
        mode = _STRATEGY_BATCH_MODES.get(str(normalized_request.get("strategy")))
        if mode is None:
            raise ValueError("Pending account history is corrupted.")
        self._check_saved_claim(
            normalized_request, mode, _BOOTSTRAP_LIMITS,
            "Pending history resolution",
        )
        if user_id is not None and owner.get("id") != user_id:
            return None
        return pending

    def _validated_pending_resolution(
        self, pending: Any
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[str]]]:
        if not isinstance(pending, dict):
            raise ValueError("Pending account history is corrupted.")
        owner = pending.get("owner")
        request = pending.get("request")
        queue_ids = pending.get("queueIds")
        if (
            not isinstance(owner, dict)
            or not isinstance(request, dict)
            or not isinstance(queue_ids, dict)
            or frozenset(queue_ids) not in _PENDING_RESOLUTION_QUEUE_DOMAINS
            or any(
                not isinstance(ids, list)
                or any(not isinstance(item_id, str) for item_id in ids)
                or len(ids) != len(set(ids))
                for ids in queue_ids.values()
            )
            or not isinstance(owner.get("id"), str)
            or not owner["id"]
            or not isinstance(request.get("requestId"), str)
            or not request["requestId"]
            or request.get("deviceId") != self._dependencies.device_id()
            or request.get("strategy") not in {"keep_remote", "replace_remote", "merge"}
        ):
            raise ValueError("Pending account history is corrupted.")
        return owner, request, queue_ids

    def _normalized_pending_resolution_request(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        normalized_request = dict(request)
        for key in ("autoStartOperations", "selectedTaskOperations"):
            if key in normalized_request:
                normalized_request[key] = self._wire_preference_operations(
                    normalized_request[key],
                    "Pending account history is corrupted.",
                )
        return normalized_request

    def clear_pending_resolution(self) -> None:
        self._dependencies.replace_meta("pendingResolution", None)

    def _ensure_no_pending_resolution(self) -> None:
        if self.pending_resolution() is not None:
            raise ValueError("Resolve pending account history before making changes.")

    def discard_pending_resolution(self, user_id: str, request_id: str) -> bool:
        with self._dependencies.transaction():
            pending = self.pending_resolution(user_id)
            if pending is None or pending["request"].get("requestId") != request_id:
                return False
            self._dependencies.write_meta("pendingResolution", None)
        return True

    def bootstrap_resolution_plan(
        self,
        response: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> dict[str, Any]:
        canonical = self._dependencies.validate_sync_response(response, self._empty_sync_request())
        with self._dependencies.transaction():
            self._dependencies.preflight_pending_queues()
            sample, anchor = self._dependencies.response_clock_sample(
                canonical["serverTimeMs"],
                request_physical_ms,
                received_physical_ms,
                request_monotonic_ms,
                received_monotonic_ms,
            )
            state = self._dependencies.load_state()
            projection = self._dependencies.project_operation(
                state["settings"],
                now=canonical["serverTime"],
            )
            local_timer = projection.canonical_timer
            local_history = projection.history
            strategy = self._bootstrap_strategy(
                state, local_timer, local_history, canonical
            )
            if sample is not None:
                self._dependencies.write_meta("serverClockSample", sample)
        if anchor is not None:
            self._dependencies.set_trusted_time_anchor(anchor)
        local_history_exists = any(
            item.get("status") == "completed" for item in local_history
        )
        remote_history_exists = any(
            item.get("status") == "completed" for item in canonical["history"]
        )
        return {
            "expectedRevision": canonical["revision"],
            "localHistory": local_history_exists,
            "remoteHistory": remote_history_exists,
            "strategy": strategy,
        }

    @staticmethod
    def _empty_sync_request() -> dict[str, list[Any]]:
        return {key: [] for key in (
            "commands", "taskOperations", "durationOperations",
            "autoStartOperations", "selectedTaskOperations",
        )}

    @staticmethod
    def _has_bootstrap_state(state: dict[str, Any], timer: Any = None) -> bool:
        settings = state.get("settings", state)
        snapshot = state.get("snapshot", state)
        queues = (
            "pending", "pendingTasks", "pendingDurations",
            "pendingAutoStarts", "pendingSelectedTasks",
        )
        return bool(
            any(state.get(key) for key in queues)
            or snapshot.get("canonicalTimer") or snapshot.get("history")
            or snapshot.get("tasks") or snapshot.get("selectedTaskId") is not None
            or settings.get("selectedTaskId") is not None
            or settings.get("autoStartBreaks")
            or any(settings.get("durationsMs", {}).get(phase)
                   != definition["default_minutes"] * 60_000
                   for phase, definition in PHASES.items())
            or timer is not None
        )

    def _bootstrap_strategy(self, state, local_timer, local_history, canonical):
        plan_input = {
            "localHistory": local_history,
            "remoteHistory": canonical["history"],
            "hasLocalState": self._has_bootstrap_state(state, local_timer),
            "hasRemoteState": self._has_bootstrap_state(canonical),
        }
        core = self._dependencies.shared_core() or _default_shared_core()
        try:
            value = core.dispatch("bootstrap.plan.v1", plan_input)
        except SharedCoreOperationError as error:
            # D68: only operation rejection is validation; load/ABI stay infra.
            raise ValueError(str(error)) from error
        return self._validated_bootstrap_plan(
            value, local_history, canonical["history"]
        )

    @staticmethod
    def _completed_history_count(history: list[dict[str, Any]]) -> int:
        identities = {
            ("timer", item["timerId"])
            if isinstance(item.get("timerId"), str) and item["timerId"]
            else ("id", item["id"])
            for item in history
            if item.get("status") == "completed"
            and (
                isinstance(item.get("timerId"), str)
                and item["timerId"]
                or isinstance(item.get("id"), str)
                and item["id"]
            )
        }
        return len(identities)

    @classmethod
    def _validated_bootstrap_plan(
        cls,
        value: object,
        local_history: list[dict[str, Any]],
        remote_history: list[dict[str, Any]],
    ) -> str | None:
        invalid = ValueError("Shared core returned an invalid bootstrap plan.")
        if not isinstance(value, dict):
            raise invalid
        mode = value.get("mode")
        if mode == "choose":
            if set(value) != {
                "mode",
                "localHistoryCount",
                "remoteHistoryCount",
            }:
                raise invalid
            local_count = value["localHistoryCount"]
            remote_count = value["remoteHistoryCount"]
            if (
                isinstance(local_count, bool)
                or not isinstance(local_count, int)
                or isinstance(remote_count, bool)
                or not isinstance(remote_count, int)
                or local_count != cls._completed_history_count(local_history)
                or remote_count != cls._completed_history_count(remote_history)
            ):
                raise invalid
            return None
        if set(value) != {"mode", "strategy", "reason"} or mode != "auto":
            raise invalid
        expected = {
            ("keep_remote", "remote_only"),
            ("keep_remote", "empty"),
            ("replace_remote", "local_only"),
            ("merge", "local_state_only"),
        }
        pair = (value.get("strategy"), value.get("reason"))
        if pair not in expected:
            raise invalid
        return str(value["strategy"])

    def prepare_resolution(
        self,
        user: dict[str, Any],
        expected_revision: int,
        strategy: str,
    ) -> dict[str, Any]:
        user_id = self._validated_resolution_identity(
            user, expected_revision, strategy
        )
        with self._dependencies.transaction():
            pending = self.pending_resolution()
            if pending is not None:
                if pending["owner"].get("id") != user_id:
                    raise ValueError(
                        "Pending account history belongs to another account."
                    )
                return pending["request"]
            if self.pending_sync() is not None:
                raise ValueError(
                    "Finish pending normal sync before resolving account history."
                )
            validated_pending = self._dependencies.preflight_pending_queues()
            state = self._dependencies.load_state(projection=True)
            outbound = self._resolution_outbound(validated_pending, strategy)
            if (
                strategy == "replace_remote"
                and self._dependencies.read_meta("autoStartLegacyDefaultUnknown", False)
                and not state["pendingAutoStarts"]
            ):
                outbound.pop("autoStartOperations")
            request = {
                "requestId": str(uuid.uuid4()),
                "deviceId": self._dependencies.device_id(),
                "expectedRevision": expected_revision,
                "strategy": strategy,
                **outbound,
            }
            queue_ids = self._resolution_queue_ids(validated_pending, strategy)
            self._dependencies.write_meta(
                "pendingResolution",
                {"owner": user, "request": request, "queueIds": queue_ids},
            )
            self._retire_outbound_proof(outbound)
        return request

    def _retire_outbound_proof(self, outbound: dict[str, Any]) -> None:
        """Retire never-sent proof for the exact published payload."""
        self._dependencies.retire_delivery_proof(
            {key: [item["id"] for item in outbound.get(key, [])]
             for key in ("commands", "taskOperations", "durationOperations",
                         "autoStartOperations", "selectedTaskOperations")
             if key in outbound}
        )

    @staticmethod
    def _validated_resolution_identity(user, expected_revision, strategy) -> str:
        if strategy not in {"keep_remote", "replace_remote", "merge"}:
            raise ValueError("Unsupported history resolution strategy.")
        if (isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or not 0 <= expected_revision <= MAX_SAFE_INTEGER):
            raise ValueError("Bootstrap returned an invalid revision.")
        user_id = user.get("id") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("Signed-in user has no stable identity.")
        return user_id

    def _legacy_resolution_outbound(self, pending, strategy):
        """Pre-Core per-domain validation; remove with the repin (R43-D02 compat)."""
        if strategy == "keep_remote":
            outbound = {key: [] for key in (
                "commands", "taskOperations", "durationOperations",
                "autoStartOperations", "selectedTaskOperations",
            )}
        else:
            outbound = {
                "commands": pending["sendableCommands"],
                "taskOperations": pending["taskOperations"],
                "durationOperations": pending["durationOperations"],
                "autoStartOperations": self._wire_preference_operations(
                    pending["autoStartOperations"],
                    "Pending auto-start operation is corrupted."),
                "selectedTaskOperations": self._wire_preference_operations(
                    pending["selectedTaskOperations"],
                    "Pending selected-task operation is corrupted."),
            }
        self._validate_legacy_resolution_counts(outbound)
        return outbound

    @staticmethod
    def _validate_legacy_resolution_counts(outbound) -> None:
        labels = dict(commands="timer commands", taskOperations="task operations",
                      durationOperations="duration operations",
                      autoStartOperations="auto-start operations",
                      selectedTaskOperations="selected-task operations")
        for key, items in outbound.items():
            if len(items) > _BOOTSTRAP_LIMITS["perDomain"]:
                raise ValueError(f"History resolution supports at most "
                                 f"{_BOOTSTRAP_LIMITS['perDomain']} {labels[key]}.")

    def _resolution_outbound(self, pending, strategy):
        if strategy == "keep_remote":
            return {key: [] for key in (
                "commands", "taskOperations", "durationOperations",
                "autoStartOperations", "selectedTaskOperations",
            )}
        device_id = self._dependencies.device_id()
        plan = self._try_batch_plan(
            {
                "kind": "new",
                "mode": _STRATEGY_BATCH_MODES[strategy],
                "limits": dict(_BOOTSTRAP_LIMITS),
                "nextDomain": "commands",
                "queues": _batch_descriptors(pending, device_id),
                "timerDependencies": _batch_timer_dependencies(
                    self._dependencies.connection
                ),
            }
        )
        if plan is None:
            return self._legacy_resolution_outbound(pending, strategy)
        if plan["status"] != "planned":
            raise ValueError(
                f"History resolution {plan['status']} exceeds Core aggregate "
                "limits or a timer dependency blocks the replacement. "
                "Retained work needs recovery."
            )
        return self._selected_sync_records(pending, plan)

    @staticmethod
    def _resolution_queue_ids(pending, strategy):
        domains = ("taskOperations", "durationOperations",
                   "autoStartOperations", "selectedTaskOperations")
        commands = pending["commands"] if strategy == "keep_remote" else pending["sendableCommands"]
        return {"commands": [item["id"] for item in commands], **{
            domain: [item["id"] for item in pending[domain]] for domain in domains
        }}
