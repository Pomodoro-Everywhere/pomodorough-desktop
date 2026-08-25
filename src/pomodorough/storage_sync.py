from __future__ import annotations

import json
import sqlite3
import uuid
from copy import deepcopy
from typing import Any, Protocol

from .core import PHASES
from .shared_core import SharedCoreError
from .storage_model import (
    MAX_SAFE_INTEGER,
    RESOLUTION_OPERATION_MAX,
    _default_shared_core,
)

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

class SyncStorageDependencies(Protocol):
    connection: sqlite3.Connection
    device_id: str
    _shared_core: Any

    def _bounded_integer(self, value: Any, label: str) -> int: ...
    def _clock_sample_for_response(self, *args: Any) -> tuple[Any, Any]: ...
    def _immediate_transaction(self) -> Any: ...
    def _preflight_pending_queues(self) -> dict[str, Any]: ...
    def _project_operation(self, *args: Any, **kwargs: Any) -> Any: ...
    def _set_meta(self, key: str, value: Any) -> None: ...
    def _set_trusted_time_anchor(self, anchor: dict[str, int]) -> None: ...
    def _validated_sync_response(
        self, response: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def get_meta(self, key: str, default: Any = None) -> Any: ...
    def load(self, *, projection: bool = False) -> dict[str, Any]: ...
    def set_meta(self, key: str, value: Any) -> None: ...


class SyncStorage:
    def __init__(self, store: SyncStorageDependencies) -> None:
        self._store = store

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
        if self._store.connection.in_transaction:
            self._store._set_meta(key, value)
        else:
            self._store.set_meta(key, value)

    def sync_payload(self) -> dict[str, Any]:
        with self._store._immediate_transaction():
            self._ensure_no_pending_resolution()
            claimed = self.pending_sync()
            if claimed is not None:
                return claimed
            pending = self._store._preflight_pending_queues()
            snapshot = self._store.get_meta("snapshot", {})
            revision = self._store._bounded_integer(
                snapshot.get("revision", 0), "Persisted revision"
            )
            payload = {
                "deviceId": self._store.device_id,
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
            self._store._set_meta("pendingSync", payload)
        return payload

    def pending_sync(self) -> dict[str, Any] | None:
        pending = self._store.get_meta("pendingSync")
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
        original = deepcopy(pending)
        if isinstance(pending, dict) and set(pending) == legacy_keys:
            pending = {**pending, "selectedTaskOperations": []}
        if (
            not isinstance(pending, dict)
            or set(pending) != current_keys
            or pending.get("deviceId") != self._store.device_id
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
        self._store._bounded_integer(
            pending.get("lastRevision"), "Pending normal sync revision"
        )
        pending = {
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
        if pending != original:
            self._replace_meta_inside_or_outside_transaction("pendingSync", pending)
        return pending

    def has_sendable_sync_operations(self) -> bool:
        with self._store._immediate_transaction():
            self._ensure_no_pending_resolution()
            pending = self._store._preflight_pending_queues()
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
            pending = self._store.get_meta("pendingResolution")
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
            or request.get("deviceId") != self._store.device_id
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
        self._store.set_meta("pendingResolution", None)

    def _ensure_no_pending_resolution(self) -> None:
        if self.pending_resolution() is not None:
            raise ValueError("Resolve pending account history before making changes.")

    def discard_pending_resolution(self, user_id: str, request_id: str) -> bool:
        with self._store._immediate_transaction():
            pending = self.pending_resolution(user_id)
            if pending is None or pending["request"].get("requestId") != request_id:
                return False
            self._store._set_meta("pendingResolution", None)
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
        canonical = self._store._validated_sync_response(response, self._empty_sync_request())
        with self._store._immediate_transaction():
            self._store._preflight_pending_queues()
            sample, anchor = self._store._clock_sample_for_response(
                canonical["serverTimeMs"],
                request_physical_ms,
                received_physical_ms,
                request_monotonic_ms,
                received_monotonic_ms,
            )
            state = self._store.load()
            projection = self._store._project_operation(
                state["settings"],
                now=canonical["serverTime"],
            )
            local_timer = projection.canonical_timer
            local_history = projection.history
            strategy = self._bootstrap_strategy(
                state, local_timer, local_history, canonical
            )
            if sample is not None:
                self._store._set_meta("serverClockSample", sample)
        if anchor is not None:
            self._store._set_trusted_time_anchor(anchor)
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
        core = self._store._shared_core or _default_shared_core()
        try:
            value = core.dispatch("bootstrap.plan.v1", plan_input)
        except SharedCoreError as error:
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
        with self._store._immediate_transaction():
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
            validated_pending = self._store._preflight_pending_queues()
            state = self._store.load(projection=True)
            outbound = self._resolution_outbound(
                validated_pending, strategy != "keep_remote"
            )
            if (
                strategy == "replace_remote"
                and self._store.get_meta("autoStartLegacyDefaultUnknown", False)
                and not state["pendingAutoStarts"]
            ):
                outbound.pop("autoStartOperations")
            self._validate_resolution_operation_counts(outbound)
            request = {
                "requestId": str(uuid.uuid4()),
                "deviceId": self._store.device_id,
                "expectedRevision": expected_revision,
                "strategy": strategy,
                **outbound,
            }
            queue_ids = self._resolution_queue_ids(validated_pending, strategy)
            self._store._set_meta(
                "pendingResolution",
                {"owner": user, "request": request, "queueIds": queue_ids},
            )
        return request

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

    def _resolution_outbound(self, pending, include_operations):
        if not include_operations:
            return {key: [] for key in (
                "commands", "taskOperations", "durationOperations",
                "autoStartOperations", "selectedTaskOperations",
            )}
        return {
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

    @staticmethod
    def _validate_resolution_operation_counts(outbound) -> None:
        labels = dict(commands="timer commands", taskOperations="task operations",
                      durationOperations="duration operations",
                      autoStartOperations="auto-start operations",
                      selectedTaskOperations="selected-task operations")
        for key, items in outbound.items():
            if len(items) > RESOLUTION_OPERATION_MAX:
                raise ValueError(f"History resolution supports at most "
                                 f"{RESOLUTION_OPERATION_MAX} {labels[key]}.")

    @staticmethod
    def _resolution_queue_ids(pending, strategy):
        domains = ("taskOperations", "durationOperations",
                   "autoStartOperations", "selectedTaskOperations")
        commands = pending["commands"] if strategy == "keep_remote" else pending["sendableCommands"]
        return {"commands": [item["id"] for item in commands], **{
            domain: [item["id"] for item in pending[domain]] for domain in domains
        }}
