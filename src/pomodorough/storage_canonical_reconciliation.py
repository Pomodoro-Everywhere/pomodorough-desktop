"""Canonical reconciliation against the authoritative SharedCore.

Production requires a v2-capable bundle (``reconcile.rebase.v2``). There is
no v1 fallback by design: a bundle that answers v2 with "unsupported
shared-core operation" fails closed, preserving queues and the claimed
request for recovery after a Core upgrade. Contract tests that need v2
today run against the test-only double in ``tests/v2_core_double.py``,
which emulates delivery mechanics on top of the real v1/projection and
must never be mistaken for authoritative Core behavior.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .core import parse_timestamp_ms
from .shared_core import SharedCoreOperationError
from .storage_model import _default_shared_core

_CORE_QUEUE_OPERATION_FIELDS = {
    "commands": frozenset(
        {
            "id",
            "deviceId",
            "deviceSequence",
            "timerId",
            "type",
            "phase",
            "plannedDurationMs",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
            "observedElapsedMs",
        }
    ),
    "taskOperations": frozenset(
        {
            "id",
            "deviceId",
            "taskId",
            "type",
            "title",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
        }
    ),
    "durationOperations": frozenset(
        {
            "id",
            "deviceId",
            "phase",
            "durationMs",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
        }
    ),
    "autoStartOperations": frozenset(
        {
            "id",
            "deviceId",
            "enabled",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
        }
    ),
    "selectedTaskOperations": frozenset(
        {
            "id",
            "deviceId",
            "taskId",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
        }
    ),
}

_QUEUE_OUTPUT_KEYS = {
    "commands": "pending",
    "taskOperations": "pendingTaskOperations",
    "durationOperations": "pendingDurationOperations",
    "autoStartOperations": "pendingAutoStartOperations",
    "selectedTaskOperations": "pendingSelectedTaskOperations",
}

_QUEUE_TABLES = {
    "commands": "pending_commands",
    "taskOperations": "pending_task_operations",
    "durationOperations": "pending_duration_operations",
    "autoStartOperations": "pending_auto_start_operations",
    "selectedTaskOperations": "pending_selected_task_operations",
}


class CanonicalReconciliationDependencies(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    @property
    def device_id(self) -> str: ...

    def shared_core(self) -> Any: ...

    def delivery_proof(self) -> dict[str, list[str]]: ...

    def never_sent_claim(self, sent: dict[str, list[str]]) -> dict[str, list[str]]: ...

    def drop_delivery_proof(self, domain: str, operation_id: str) -> None: ...

    def _command_physical_times(self) -> dict[str, int]: ...

    def _normalize_settings(self, settings: Any) -> dict[str, Any]: ...

    def _preflight_pending_queues(
        self, *, require_clock_coverage: bool = True
    ) -> dict[str, list[dict[str, Any]]]: ...

    def _set_meta(self, key: str, value: Any) -> None: ...

    def _validated_projection_state(
        self, projection: dict[str, Any], *, context: str
    ) -> tuple[
        dict[str, Any] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, int],
        bool,
        str | None,
    ]: ...

    def get_meta(self, key: str, default: Any = None) -> Any: ...


class CanonicalReconciliationHooks(Protocol):
    def _pending_generated_break_metadata(self) -> dict[str, Any]: ...

    def _core_timer_dependency(
        self,
        row: Any,
        commands: dict[str, dict[str, Any]],
        generated: dict[str, Any],
        physical_times: dict[str, int],
    ) -> dict[str, Any]: ...

    def _generated_break_day_bounds(self, source_ms: int) -> tuple[str, str]: ...

    def _core_canonical_response(self, canonical: dict[str, Any]) -> dict[str, Any]: ...

    def _core_timer_dependencies(
        self, pending: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]: ...

    def _core_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def _core_canonical_timer(self, timer: Any) -> dict[str, Any] | None: ...

    def _reconciliation_output_fields(self) -> set[str]: ...

    def _normalized_reconciliation_queues(
        self,
        value: dict[str, Any],
        output_keys: dict[str, str],
        invalid: ValueError,
    ) -> dict[str, list[dict[str, Any]]]: ...

    def _validated_reconciliation_id_sets(
        self,
        value: dict[str, Any],
        local_commands: dict[str, dict[str, Any]],
        canonical: dict[str, Any],
        invalid: ValueError,
    ) -> tuple[list[str], list[str], list[str]]: ...

    def _validate_reconciliation_queues(
        self,
        normalized: dict[str, list[dict[str, Any]]],
        local: dict[str, dict[str, dict[str, Any]]],
        canonical: dict[str, Any],
        dropped: set[str],
        invalid: ValueError,
    ) -> None: ...

    def _validated_reconciliation_dependencies(
        self,
        dependencies: Any,
        commands: list[dict[str, Any]],
        invalid: ValueError,
    ) -> list[dict[str, Any]]: ...

    def _validated_reconciliation_projection(
        self,
        value: dict[str, Any],
        canonical: dict[str, Any],
        invalid: ValueError,
    ) -> dict[str, Any]: ...

    def _normalized_core_queue_operation(
        self, domain: str, value: Any
    ) -> dict[str, Any]: ...

    def _persist_reconciliation_queue(
        self,
        table: str,
        originals: list[dict[str, Any]],
        retained_operations: list[dict[str, Any]],
    ) -> None: ...

    def _persist_reconciliation_dependencies(self, result: dict[str, Any]) -> None: ...

    def _reconcile_removed_auto_break_starts(
        self,
        result: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None: ...

    def _core_reconciliation_input(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]: ...

    def _validated_reconciliation_output(
        self,
        value: object,
        canonical: dict[str, Any],
        request: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]: ...

    def _persist_core_reconciliation(
        self,
        result: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None: ...


def generated_break_day_bounds(source_ms: int) -> tuple[str, str]:
    source_day = (
        datetime.fromtimestamp(source_ms / 1000, UTC)
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start = (
        source_day.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    end = (
        (source_day + timedelta(days=1))
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return start, end


def core_canonical_timer(timer: Any) -> dict[str, Any] | None:
    if timer is None:
        return None
    normalized = {
        key: timer[key]
        for key in (
            "id",
            "phase",
            "status",
            "plannedDurationMs",
            "elapsedAtAnchorMs",
            "anchorAt",
        )
    }
    for key in ("taskId", "lastIntent"):
        if timer.get(key) is not None:
            normalized[key] = timer[key]
    if timer.get("startedByDeviceId"):
        normalized["startedByDeviceId"] = timer["startedByDeviceId"]
    return normalized


def core_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in history:
        core_item = {
            key: item[key]
            for key in ("id", "timerId", "phase", "status", "plannedDurationMs")
        }
        for key in ("taskId", "commandId", "completedAt", "endedAt"):
            if item.get(key) is not None:
                core_item[key] = item[key]
        normalized.append(core_item)
    return normalized


def reconciliation_output_fields() -> set[str]:
    return {
        "revision",
        "pending",
        "pendingTaskOperations",
        "pendingDurationOperations",
        "pendingAutoStartOperations",
        "pendingSelectedTaskOperations",
        "pendingTimerDependencies",
        "promotedTimerOperationIds",
        "droppedTimerOperationIds",
        "droppedTimerIds",
        "baseTimer",
        "baseHistory",
        "baseTasks",
        "baseDurationsMs",
        "baseAutoStartBreaks",
        "baseSelectedTaskId",
        "timer",
        "history",
        "tasks",
        "durationsMs",
        "autoStartBreaks",
        "selectedTaskId",
        "projectionPending",
    }


def validated_reconciliation_id_sets(
    value: dict[str, Any],
    local_commands: dict[str, dict[str, Any]],
    canonical: dict[str, Any],
    invalid: ValueError,
) -> tuple[list[str], list[str], list[str]]:
    dropped = value["droppedTimerOperationIds"]
    promoted = value["promotedTimerOperationIds"]
    timer_ids = value["droppedTimerIds"]
    if any(
        not isinstance(items, list)
        or any(not isinstance(item, str) or not item for item in items)
        or len(items) != len(set(items))
        for items in (dropped, promoted, timer_ids)
    ):
        raise invalid
    available = set(local_commands) - {
        item["commandId"] for item in canonical["acknowledgements"]
    }
    if not set(dropped) <= available or not set(promoted) <= available - set(dropped):
        raise invalid
    return dropped, promoted, timer_ids


def validated_reconciliation_dependencies(
    dependencies: Any,
    commands: list[dict[str, Any]],
    invalid: ValueError,
) -> list[dict[str, Any]]:
    if not isinstance(dependencies, list):
        raise invalid
    children, retained = set(), {item["id"] for item in commands}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise invalid
        generated = dependency.get("generatedBreak", False)
        keys = {"operationId", "dependsOnOperationId"}
        if generated:
            keys |= {"generatedBreak", "sourceDayStart", "sourceDayEnd"}
        child = dependency.get("operationId")
        parent = dependency.get("dependsOnOperationId")
        bad_dates = generated and (
            not isinstance(dependency.get("sourceDayStart"), str)
            or not isinstance(dependency.get("sourceDayEnd"), str)
        )
        if (
            set(dependency) != keys
            or not isinstance(generated, bool)
            or child not in retained
            or parent not in retained
            or child == parent
            or child in children
            or bad_dates
        ):
            raise invalid
        children.add(child)
    return dependencies


class SharedCoreReconciliationAdapter:
    def __init__(
        self,
        dependencies: CanonicalReconciliationDependencies,
        hooks: CanonicalReconciliationHooks,
    ) -> None:
        self._dependencies = dependencies
        self._hooks = hooks

    def _core_timer_dependencies(
        self, pending: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        commands = {str(command["id"]): command for command in pending["commands"]}
        generated = self._hooks._pending_generated_break_metadata()
        physical_times = self._dependencies._command_physical_times()
        rows = self._dependencies.connection.execute(
            "SELECT id, depends_on_command_id FROM pending_commands "
            "WHERE depends_on_command_id IS NOT NULL ORDER BY device_sequence"
        )
        dependencies = [
            self._hooks._core_timer_dependency(row, commands, generated, physical_times)
            for row in rows
        ]
        if set(generated) - {item["operationId"] for item in dependencies}:
            raise ValueError("Pending generated-break metadata is invalid.")
        return dependencies

    def _pending_generated_break_metadata(self) -> dict[str, Any]:
        return {
            str(row["start_command_id"]): row
            for row in self._dependencies.connection.execute(
                "SELECT source_finish_command_id, source_timer_id, start_command_id, "
                "selected_phase_version FROM pending_auto_break_starts"
            )
        }

    def _core_timer_dependency(
        self,
        row: Any,
        commands: dict[str, dict[str, Any]],
        generated: dict[str, Any],
        physical_times: dict[str, int],
    ) -> dict[str, Any]:
        operation_id = str(row["id"])
        parent_id = str(row["depends_on_command_id"])
        if operation_id not in commands or parent_id not in commands:
            raise ValueError("Pending timer dependency metadata is invalid.")
        dependency: dict[str, Any] = {
            "operationId": operation_id,
            "dependsOnOperationId": parent_id,
        }
        metadata = generated.get(operation_id)
        if metadata is None:
            return dependency
        if str(metadata["source_finish_command_id"]) != parent_id:
            raise ValueError("Pending generated-break metadata is invalid.")
        source_ms = physical_times.get(parent_id)
        if source_ms is None:
            source_ms = parse_timestamp_ms(
                str(commands[parent_id].get("occurredAt", ""))
            )
        if source_ms is None:
            raise ValueError("Pending generated-break metadata is invalid.")
        source_day_start, source_day_end = self._hooks._generated_break_day_bounds(
            source_ms
        )
        dependency.update(
            generatedBreak=True,
            sourceDayStart=source_day_start,
            sourceDayEnd=source_day_end,
        )
        return dependency

    def _core_reconciliation_input(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        pending = self._dependencies._preflight_pending_queues(require_clock_coverage=False)

        def with_device_id(operation: dict[str, Any]) -> dict[str, Any]:
            item = dict(operation)
            item.setdefault("deviceId", self._dependencies.device_id)
            return item

        queue_keys = tuple(_QUEUE_OUTPUT_KEYS)
        response = self._hooks._core_canonical_response(canonical)
        sent = {
            key: [{"id": item["id"]} for item in request.get(key, [])]
            for key in queue_keys
        }
        sent_ids = {
            key: [item["id"] for item in sent[key]] for key in queue_keys
        }
        never_sent = self._dependencies.never_sent_claim(sent_ids)
        local_ids = {
            key: {str(item["id"]) for item in pending[key]} for key in queue_keys
        }
        never_sent = {
            domain: [item for item in ids if item in local_ids.get(domain, set())]
            for domain, ids in never_sent.items()
        }
        never_sent = {
            domain: ids for domain, ids in never_sent.items() if ids
        }
        return (
            {
                "local": {
                    key: [with_device_id(item) for item in pending[key]]
                    for key in queue_keys
                },
                "sent": sent,
                "neverSent": never_sent,
                "response": response,
                "timerDependencies": self._hooks._core_timer_dependencies(pending),
            },
            pending,
        )

    def _core_canonical_response(self, canonical: dict[str, Any]) -> dict[str, Any]:
        history = self._hooks._core_history(canonical["history"])
        canonical_timer = self._hooks._core_canonical_timer(canonical["canonicalTimer"])
        if canonical_timer is not None and any(
            item["timerId"] == canonical_timer["id"] for item in history
        ):
            # Desktop retains terminal timer beside history for dismissal UI.
            canonical_timer = None
        return {
            "acknowledgements": canonical["acknowledgements"],
            "taskAcknowledgements": canonical["taskAcknowledgements"],
            "durationAcknowledgements": canonical["durationAcknowledgements"],
            "autoStartAcknowledgements": canonical["autoStartAcknowledgements"],
            "selectedTaskAcknowledgements": canonical["selectedTaskAcknowledgements"],
            "revision": canonical["revision"],
            "canonicalTimer": canonical_timer,
            "history": history,
            "tasks": [
                {"id": task["id"], "title": task["title"]}
                for task in canonical["tasks"]
            ],
            "durationsMs": canonical["durationsMs"],
            "autoStartBreaks": canonical["autoStartBreaks"],
            "selectedTaskId": canonical["selectedTaskId"],
            "serverTime": canonical["serverTime"],
            "serverHlcWallMs": canonical["serverHlcWallMs"],
            "serverHlcCounter": canonical["serverHlcCounter"],
        }

    def _normalized_core_queue_operation(
        self,
        domain: str,
        value: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or value.get("deviceId") != self._dependencies.device_id
        ):
            raise ValueError("Shared core returned invalid reconciliation queues.")
        required = _CORE_QUEUE_OPERATION_FIELDS[domain]
        allowed = set(required)
        if domain == "commands":
            allowed.add("taskId")
        if set(value) < required or set(value) - allowed:
            raise ValueError("Shared core returned invalid reconciliation queues.")
        operation = dict(value)
        if domain in {"commands", "taskOperations", "durationOperations"}:
            operation.pop("deviceId")
        if (
            domain == "taskOperations"
            and operation.get("type") == "delete"
            and operation.get("title") == ""
        ):
            operation.pop("title")
        return operation

    def _validated_reconciliation_output(
        self,
        value: object,
        canonical: dict[str, Any],
        request: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        invalid = ValueError("Shared core returned invalid reconciliation output.")
        required = self._hooks._reconciliation_output_fields()
        if not isinstance(value, dict) or set(value) != required:
            raise invalid
        if value["revision"] != canonical["revision"]:
            raise invalid
        normalized = self._hooks._normalized_reconciliation_queues(
            value, _QUEUE_OUTPUT_KEYS, invalid
        )
        projection_pending = self._validated_projection_pending(
            value, normalized, invalid
        )
        local = {
            domain: {str(item["id"]): item for item in pending[domain]}
            for domain in _QUEUE_OUTPUT_KEYS
        }
        dropped, promoted, timer_ids = self._hooks._validated_reconciliation_id_sets(
            value, local["commands"], canonical, invalid
        )
        self._hooks._validate_reconciliation_queues(
            normalized, local, canonical, set(dropped), invalid
        )
        dependencies = self._hooks._validated_reconciliation_dependencies(
            value["pendingTimerDependencies"], normalized["commands"], invalid
        )
        projection = self._hooks._validated_reconciliation_projection(
            value, canonical, invalid
        )
        return {
            "queues": normalized,
            "projectionPending": projection_pending,
            "dependencies": dependencies,
            "promoted": set(promoted),
            "dropped": set(dropped),
            "droppedTimerIds": set(timer_ids),
            "projection": projection,
        }

    def _validated_projection_pending(
        self,
        value: dict[str, Any],
        normalized: dict[str, list[dict[str, Any]]],
        invalid: ValueError,
    ) -> dict[str, list[dict[str, Any]]]:
        """Validate v2 safe optimistic queues against retained pending.

        Each domain is either the full retained queue or empty. A partial
        safe subset is never valid; one unsafe operation excludes the
        whole domain without deleting it from pending.
        """
        raw = value.get("projectionPending")
        if not isinstance(raw, dict) or set(raw) != set(_QUEUE_OUTPUT_KEYS):
            raise invalid
        validated: dict[str, list[dict[str, Any]]] = {}
        try:
            for domain in _QUEUE_OUTPUT_KEYS:
                items = raw[domain]
                if not isinstance(items, list):
                    raise invalid
                retained = {str(item["id"]): item for item in normalized[domain]}
                if not items:
                    validated[domain] = []
                    continue
                if len(items) != len(normalized[domain]):
                    raise invalid
                checked = [
                    self._hooks._normalized_core_queue_operation(domain, item)
                    for item in items
                ]
                for item in checked:
                    original = retained.get(str(item["id"]))
                    if original is None or original != item:
                        raise invalid
                validated[domain] = checked
        except ValueError as error:
            raise invalid from error
        return validated

    def _normalized_reconciliation_queues(
        self,
        value: dict[str, Any],
        output_keys: dict[str, str],
        invalid: ValueError,
    ) -> dict[str, list[dict[str, Any]]]:
        normalized = {}
        try:
            for domain, output_key in output_keys.items():
                if not isinstance(value[output_key], list):
                    raise invalid
                normalized[domain] = [
                    self._hooks._normalized_core_queue_operation(domain, item)
                    for item in value[output_key]
                ]
        except ValueError as error:
            raise invalid from error
        return normalized

    def _validated_reconciliation_projection(
        self,
        value: dict[str, Any],
        canonical: dict[str, Any],
        invalid: ValueError,
    ) -> dict[str, Any]:
        projection = {
            "canonicalTimer": value["timer"],
            "history": value["history"],
            "tasks": value["tasks"],
            "durationsMs": value["durationsMs"],
            "autoStartBreaks": value["autoStartBreaks"],
            "selectedTaskId": value["selectedTaskId"],
        }
        self._dependencies._validated_projection_state(projection, context="reconciliation")
        base = self._hooks._core_canonical_response(canonical)
        pairs = (
            ("baseTimer", "canonicalTimer"),
            ("baseHistory", "history"),
            ("baseTasks", "tasks"),
            ("baseDurationsMs", "durationsMs"),
            ("baseAutoStartBreaks", "autoStartBreaks"),
            ("baseSelectedTaskId", "selectedTaskId"),
        )
        if any(value[source] != base[target] for source, target in pairs):
            raise invalid
        return projection

    def _persist_core_reconciliation(
        self,
        result: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None:
        for domain, table in _QUEUE_TABLES.items():
            self._hooks._persist_reconciliation_queue(
                table, pending[domain], result["queues"][domain]
            )
        self._drop_consumed_delivery_proof(result, pending)
        self._hooks._persist_reconciliation_dependencies(result)
        self._hooks._reconcile_removed_auto_break_starts(result, pending)
        self._dependencies._preflight_pending_queues(require_clock_coverage=False)

    def _drop_consumed_delivery_proof(
        self,
        result: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None:
        retained_ids = {
            domain: {str(item["id"]) for item in result["queues"][domain]}
            for domain in _QUEUE_TABLES
        }
        for domain, operations in pending.items():
            for operation in operations:
                operation_id = str(operation["id"])
                if operation_id not in retained_ids.get(domain, set()):
                    self._dependencies.drop_delivery_proof(domain, operation_id)

    def _persist_reconciliation_queue(
        self,
        table: str,
        originals: list[dict[str, Any]],
        retained_operations: list[dict[str, Any]],
    ) -> None:
        """Consume acknowledgements without rewriting retained operations.

        Retained operations keep their exact identities, clocks, and
        occurrences for durable retry. Core's accepted generated-break
        normalization may adjust a retained break batch's phase, planned
        duration, and observed elapsed time; any other retained rewrite
        is a delivery-policy error, not an update.
        """
        allowed = (
            {"phase", "plannedDurationMs", "observedElapsedMs"}
            if table == "pending_commands"
            else set()
        )
        retained = {
            str(operation["id"]): operation for operation in retained_operations
        }
        for original in originals:
            operation_id = str(original["id"])
            operation = retained.get(operation_id)
            if operation is None:
                self._dependencies.connection.execute(
                    f"DELETE FROM {table} WHERE id = ?", (operation_id,)
                )
            elif any(
                original.get(key) != operation.get(key)
                for key in (set(original) | set(operation)) - allowed
            ):
                raise ValueError(
                    "Shared core rewrote a retained operation payload."
                )
            elif operation != original:
                payload = json.dumps(operation, separators=(",", ":"))
                self._dependencies.connection.execute(
                    f"UPDATE {table} SET payload = ? WHERE id = ?",
                    (payload, operation_id),
                )

    def _persist_reconciliation_dependencies(self, result: dict[str, Any]) -> None:
        parents = {
            item["operationId"]: item["dependsOnOperationId"]
            for item in result["dependencies"]
        }
        for operation in result["queues"]["commands"]:
            self._dependencies.connection.execute(
                "UPDATE pending_commands SET depends_on_command_id = ? WHERE id = ?",
                (parents.get(operation["id"]), operation["id"]),
            )

    def _reconcile_removed_auto_break_starts(
        self,
        result: dict[str, Any],
        pending: dict[str, list[dict[str, Any]]],
    ) -> None:
        generated = {
            dependency["operationId"]
            for dependency in result["dependencies"]
            if dependency.get("generatedBreak", False)
        }
        rows = self._dependencies.connection.execute(
            "SELECT source_finish_command_id, start_command_id, "
            "selected_phase_version FROM pending_auto_break_starts"
        ).fetchall()
        before = {item["id"]: item for item in pending["commands"]}
        after = {item["id"]: item for item in result["queues"]["commands"]}
        for row in rows:
            self._reconcile_removed_auto_break_start(
                row, generated, before, after, result
            )

    def _reconcile_removed_auto_break_start(
        self,
        row: sqlite3.Row,
        generated: set[str],
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        result: dict[str, Any],
    ) -> None:
        start_id = str(row["start_command_id"])
        if start_id in generated:
            return
        source_id = str(row["source_finish_command_id"])
        self._dependencies.connection.execute(
            "DELETE FROM pending_auto_break_starts WHERE source_finish_command_id = ?",
            (source_id,),
        )
        self._dependencies.connection.execute(
            "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
            (source_id,),
        )
        operation = before.get(start_id)
        if operation is None:
            return
        settings = self._dependencies._normalize_settings(self._dependencies.get_meta("settings", {}))
        if settings["selectedPhase"] == operation.get("phase") and int(
            self._dependencies.get_meta("selectedPhaseVersion", 0)
        ) == int(row["selected_phase_version"]):
            if start_id in result["promoted"] and start_id in after:
                settings["selectedPhase"] = after[start_id]["phase"]
            elif start_id in result["dropped"]:
                settings["selectedPhase"] = "focus"
            self._dependencies._set_meta("settings", settings)

    def _reconcile_with_shared_core(
        self,
        canonical: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        input_value, pending = self._hooks._core_reconciliation_input(
            canonical, request
        )
        core = self._dependencies.shared_core() or _default_shared_core()
        try:
            value = core.dispatch("reconcile.rebase.v2", input_value)
        except SharedCoreOperationError as error:
            # D68: only operation rejection is validation; load/ABI stay infra.
            # Delivery-policy and causal-order errors preserve queues and
            # the claimed request for recovery; never retry a modified
            # payload under an existing ID.
            # Immutable contract: no v1 fallback. An unsupported-v2 bundle
            # fails closed until the pinned Core is upgraded.
            if "unsupported shared-core operation" in str(error.detail):
                raise ValueError(
                    "SharedCore bundle lacks reconcile.rebase.v2 "
                    f"({error.detail}); upgrade pinned Core, "
                    "no v1 fallback by design."
                ) from error
            raise ValueError(str(error)) from error
        result = self._hooks._validated_reconciliation_output(
            value, canonical, request, pending
        )
        self._hooks._persist_core_reconciliation(result, pending)
        return result
