"""Thread-safe host adapter for the pinned shared-core WebAssembly ABI."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from threading import Lock
from typing import Any, Final, Protocol, Self

from wasmtime import Engine, Func, Instance, Memory, Module, Store, WasmtimeError

CORE_COMMIT: Final = "49efee8c5ac390d5dd7bd5c1a3537fb889fa6f10"
CORE_SHA256: Final = "50519a0c12b0e38d3281d2205f5597f03bb5e8cdd7e9e57f86bb4458fd0dad64"
WASM_RESOURCE: Final = "pomodorough_core.wasm"

_MAX_OPERATION_BYTES: Final = 256
_MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
_MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_MAX_MEMORY_BYTES: Final = 256 * 1024 * 1024
_UINT32_MASK: Final = (1 << 32) - 1
_UINT64_MASK: Final = (1 << 64) - 1


class SharedCoreError(RuntimeError):
    """Base error for shared-core host failures."""


class SharedCoreLoadError(SharedCoreError):
    """Pinned module could not be verified or instantiated."""


class SharedCoreABIError(SharedCoreError):
    """Pinned module violated its host ABI contract."""


class SharedCoreOperationError(SharedCoreError):
    """Shared core rejected an operation."""

    def __init__(self, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"shared-core operation {operation} failed: {detail}")


class SharedCoreDispatcher(Protocol):
    def dispatch(self, operation: str, input_value: object) -> object: ...


@dataclass(frozen=True)
class ProjectionWinningOperationIds:
    tasks: dict[str, str]
    durations: dict[str, str]
    auto_start: str | None
    selected_task: str | None


@dataclass(frozen=True)
class ProjectionApplyV2:
    """Validated, typed output from SharedCore ``projection.apply.v2``."""

    canonical_timer: dict[str, Any] | None
    history: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    durations_ms: dict[str, int]
    auto_start_breaks: bool
    selected_task_id: str | None
    timer_outcomes: dict[str, dict[str, str]]
    winning_operation_ids: ProjectionWinningOperationIds

    def require_applied(self, command_id: str) -> None:
        outcome = self.timer_outcomes.get(command_id)
        if outcome is None or outcome["outcome"] != "applied":
            raise SharedCoreABIError(
                f"projection.apply.v2 rejected timer command {command_id}"
            )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


@dataclass
class _DispatchBuffers:
    operation: bytes
    input_value: bytes
    operation_pointer: int = 0
    input_pointer: int = 0
    result_pointer: int = 0
    result_length: int = 0

    def execute(self, core: SharedCore, operation: str) -> object:
        self.operation_pointer = core._allocate(self.operation)
        self.input_pointer = core._allocate(self.input_value)
        packed_result = core._dispatch_export(
            core._store,
            _signed_i32(self.operation_pointer),
            len(self.operation),
            _signed_i32(self.input_pointer),
            len(self.input_value),
        )
        self._capture_result(core, packed_result)
        result_bytes = bytes(
            core._memory.read(
                core._store,
                self.result_pointer,
                self.result_pointer + self.result_length,
            )
        )
        try:
            envelope_json = result_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as cause:
            raise SharedCoreABIError("dispatch result is not UTF-8") from cause
        return core._parse_envelope(operation, envelope_json)

    def _capture_result(self, core: SharedCore, packed_result: object) -> None:
        if not isinstance(packed_result, int):
            raise SharedCoreABIError("pomodorough_dispatch did not return an i64")
        packed_bits = packed_result & _UINT64_MASK
        self.result_pointer = packed_bits & _UINT32_MASK
        self.result_length = packed_bits >> 32
        if self.result_pointer == 0 and self.result_length != 0:
            raise SharedCoreABIError(
                "dispatch result has a null pointer with nonzero length"
            )
        if self.result_length == 0:
            raise SharedCoreABIError("dispatch returned an empty result buffer")
        if self.result_length > _MAX_OUTPUT_BYTES:
            raise SharedCoreABIError(
                f"dispatch result is too large: {self.result_length} bytes"
            )
        core._require_range(self.result_pointer, self.result_length, "dispatch result")

    def release(self, core: SharedCore) -> list[BaseException]:
        errors: list[BaseException] = []
        for pointer, length in (
            (self.result_pointer, self.result_length),
            (self.input_pointer, len(self.input_value)),
            (self.operation_pointer, len(self.operation)),
        ):
            try:
                core._release(pointer, length)
            except BaseException as error:
                errors.append(error)
        return errors


class SharedCore:
    """Locked adapter around shared core exports and linear memory."""

    def __init__(self) -> None:
        self._initialize(_read_packaged_wasm())

    @classmethod
    def load(cls, wasm: bytes) -> Self:
        """Verify and instantiate pinned module bytes."""
        core = cls.__new__(cls)
        core._initialize(bytes(wasm))
        return core

    def _initialize(self, wasm: bytes) -> None:
        actual_hash = hashlib.sha256(wasm).hexdigest()
        if actual_hash != CORE_SHA256:
            raise SharedCoreLoadError(
                f"shared-core SHA-256 mismatch: expected {CORE_SHA256}, got {actual_hash}"
            )

        try:
            engine = Engine()
            store = Store(engine)
            store.set_limits(memory_size=_MAX_MEMORY_BYTES, memories=1, instances=1)
            module = Module(engine, wasm)
            instance = Instance(store, module, [])
        except WasmtimeError as cause:
            raise SharedCoreLoadError("failed to instantiate shared core") from cause

        self._store = store
        self._instance = instance
        self._lock = Lock()
        self._unusable_cause: BaseException | None = None
        self._memory = self._require_memory("memory")
        self._allocate_export = self._require_func(
            "pomodorough_alloc", ("i32",), ("i32",)
        )
        self._free_export = self._require_func("pomodorough_free", ("i32", "i32"), ())
        self._dispatch_export = self._require_func(
            "pomodorough_dispatch",
            ("i32", "i32", "i32", "i32"),
            ("i64",),
        )

    def dispatch(self, operation: str, input_value: object) -> object:
        """Encode input as JSON, call pinned WASM, and return envelope value."""
        try:
            input_json = json.dumps(
                input_value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as cause:
            raise ValueError("shared-core input is not valid JSON") from cause
        return self.dispatch_json(operation, input_json)

    def apply_projection_v2(self, input_value: object) -> ProjectionApplyV2:
        """Dispatch and strictly validate synchronized Desktop state."""
        return apply_projection_v2(self, input_value)

    def dispatch_json(self, operation: str, input_json: str) -> object:
        """Call pinned WASM with an already serialized JSON value."""
        if not isinstance(operation, str):
            raise TypeError("shared-core operation must be a string")
        if not isinstance(input_json, str):
            raise TypeError("shared-core input JSON must be a string")

        operation_bytes = operation.encode("utf-8")
        input_bytes = input_json.encode("utf-8")
        if not operation_bytes:
            raise ValueError("shared-core operation must not be empty")
        if not input_bytes:
            raise ValueError("shared-core input must not be empty")
        if len(operation_bytes) > _MAX_OPERATION_BYTES:
            raise ValueError("shared-core operation is too large")
        if len(input_bytes) > _MAX_INPUT_BYTES:
            raise ValueError("shared-core input is too large")

        with self._lock:
            if self._unusable_cause is not None:
                raise SharedCoreABIError(
                    "shared-core instance is unusable after cleanup failure"
                ) from self._unusable_cause
            return self._dispatch_locked(operation, operation_bytes, input_bytes)

    def _dispatch_locked(
        self, operation: str, operation_bytes: bytes, input_bytes: bytes
    ) -> object:
        buffers = _DispatchBuffers(operation_bytes, input_bytes)
        value: object = None
        primary: BaseException | None = None
        try:
            value = buffers.execute(self, operation)
        except BaseException as cause:
            primary = self._dispatch_failure(cause)
        return self._complete_dispatch(value, primary, buffers.release(self))

    @staticmethod
    def _dispatch_failure(cause: BaseException) -> BaseException:
        if isinstance(cause, SharedCoreError):
            return cause
        if isinstance(cause, (IndexError, WasmtimeError)):
            error = SharedCoreABIError("shared-core ABI call failed")
            error.__cause__ = cause
            return error
        return cause

    def _complete_dispatch(
        self,
        value: object,
        primary: BaseException | None,
        cleanup_errors: list[BaseException],
    ) -> object:
        if cleanup_errors:
            self._unusable_cause = cleanup_errors[0]
            if primary is not None:
                for cleanup in cleanup_errors:
                    primary.add_note(f"shared-core cleanup failed: {cleanup!r}")
                raise primary
            error = SharedCoreABIError("shared-core cleanup failed")
            for cleanup in cleanup_errors[1:]:
                error.add_note(f"additional cleanup failure: {cleanup!r}")
            raise error from cleanup_errors[0]
        if primary is not None:
            raise primary
        return value

    def _allocate(self, value: bytes) -> int:
        raw_pointer = self._allocate_export(self._store, len(value))
        if not isinstance(raw_pointer, int):
            raise SharedCoreABIError("pomodorough_alloc did not return an i32")
        pointer = raw_pointer & _UINT32_MASK
        if pointer == 0:
            raise SharedCoreABIError("pomodorough_alloc returned a null pointer")
        self._require_range(pointer, len(value), "allocated input")
        try:
            written = self._memory.write(self._store, value, pointer)
            if written != len(value):
                raise SharedCoreABIError("linear-memory input write was incomplete")
        except BaseException as primary:
            try:
                self._release(pointer, len(value))
            except BaseException as cleanup:
                self._unusable_cause = cleanup
                primary.add_note(f"shared-core cleanup failed: {cleanup!r}")
            raise
        return pointer

    def _release(self, pointer: int, length: int) -> None:
        if pointer == 0 and length == 0:
            return
        if pointer == 0 or length == 0:
            raise SharedCoreABIError(
                "cannot release buffer with inconsistent pointer and length"
            )
        self._free_export(self._store, _signed_i32(pointer), length)

    def _require_range(self, pointer: int, length: int, label: str) -> None:
        memory_bytes = self._memory.data_len(self._store)
        if (
            pointer < 0
            or pointer > _UINT32_MASK
            or length < 0
            or pointer + length > memory_bytes
        ):
            raise SharedCoreABIError(
                f"{label} range is outside linear memory: "
                f"pointer={pointer} length={length}"
            )

    def _require_memory(self, name: str) -> Memory:
        export = self._export(name)
        if not isinstance(export, Memory):
            raise SharedCoreABIError(f"shared-core export {name} is not memory")
        return export

    def _require_func(
        self, name: str, parameters: tuple[str, ...], results: tuple[str, ...]
    ) -> Func:
        export = self._export(name)
        if not isinstance(export, Func):
            raise SharedCoreABIError(f"shared-core export {name} is not a function")
        function_type = export.type(self._store)
        actual_parameters = tuple(map(str, function_type.params))
        actual_results = tuple(map(str, function_type.results))
        if actual_parameters != parameters or actual_results != results:
            raise SharedCoreABIError(
                f"shared-core export {name} has type "
                f"{actual_parameters} -> {actual_results}; "
                f"expected {parameters} -> {results}"
            )
        return export

    def _export(self, name: str) -> object:
        try:
            return self._instance.exports(self._store)[name]
        except KeyError as cause:
            raise SharedCoreABIError(f"missing shared-core export: {name}") from cause

    @staticmethod
    def _parse_envelope(operation: str, envelope_json: str) -> object:
        try:
            envelope = json.loads(
                envelope_json,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as cause:
            raise SharedCoreABIError(
                "dispatch returned an invalid JSON envelope"
            ) from cause
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise SharedCoreABIError("dispatch envelope has no boolean ok field")
        if envelope["ok"]:
            if set(envelope) != {"ok", "value"}:
                raise SharedCoreABIError("successful dispatch envelope is malformed")
            return envelope["value"]
        if set(envelope) != {"ok", "error"}:
            raise SharedCoreABIError("failed dispatch envelope is malformed")
        detail = envelope["error"]
        if not isinstance(detail, str) or not detail:
            raise SharedCoreABIError("failed dispatch envelope has no error")
        raise SharedCoreOperationError(operation, detail)


_PROJECTION_KEYS: Final = {
    "canonicalTimer",
    "history",
    "tasks",
    "durationsMs",
    "autoStartBreaks",
    "selectedTaskId",
    "timerOutcomes",
    "winningOperationIds",
}
_PHASES: Final = {"focus", "short_break", "long_break"}
_TIMER_STATUSES: Final = {
    "running",
    "paused",
    "completed",
    "cancelled",
    "superseded",
}
_TERMINAL_STATUSES: Final = {"completed", "cancelled", "superseded"}
_OUTCOMES: Final = {"applied", "ignored", "rejected"}
_MIN_DURATION_MS: Final = 60_000
_MAX_DURATION_MS: Final = 14_400_000


def apply_projection_v2(
    dispatcher: SharedCoreDispatcher, input_value: object
) -> ProjectionApplyV2:
    """Use one strict adapter for production and injected SharedCore dispatchers."""
    value = dispatcher.dispatch("projection.apply.v2", input_value)
    return _validated_projection(value, input_value)


def _projection_error(detail: str) -> SharedCoreABIError:
    return SharedCoreABIError(f"malformed projection.apply.v2 output: {detail}")


def _exact_object(
    value: object,
    required: set[str],
    *,
    optional: set[str] | None = None,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _projection_error(f"{label} is not an object")
    allowed = required | (optional or set())
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise _projection_error(f"{label} has invalid fields")
    if not all(isinstance(key, str) for key in value):
        raise _projection_error(f"{label} has a non-string field")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _projection_error(f"{label} is not a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _projection_error(f"{label} is not an integer")
    return value


def _timestamp(value: object, label: str) -> str:
    timestamp = _nonempty_string(value, label)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as cause:
        raise _projection_error(f"{label} is not RFC 3339") from cause
    if parsed.tzinfo is None:
        raise _projection_error(f"{label} has no UTC offset")
    return timestamp


def _duration(value: object, label: str) -> int:
    duration = _integer(value, label)
    if not _MIN_DURATION_MS <= duration <= _MAX_DURATION_MS:
        raise _projection_error(f"{label} is outside supported range")
    return duration


def _validated_timer(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    timer = _exact_object(
        value,
        {
            "id",
            "phase",
            "status",
            "plannedDurationMs",
            "elapsedAtAnchorMs",
            "anchorAt",
        },
        optional={"taskId", "startedByDeviceId", "lastIntent"},
        label="canonicalTimer",
    )
    _nonempty_string(timer["id"], "canonicalTimer.id")
    if timer["phase"] not in _PHASES:
        raise _projection_error("canonicalTimer.phase is invalid")
    if timer["status"] not in _TIMER_STATUSES:
        raise _projection_error("canonicalTimer.status is invalid")
    planned = _duration(timer["plannedDurationMs"], "canonicalTimer.plannedDurationMs")
    elapsed = _integer(timer["elapsedAtAnchorMs"], "canonicalTimer.elapsedAtAnchorMs")
    if not 0 <= elapsed <= planned:
        raise _projection_error("canonicalTimer.elapsedAtAnchorMs is invalid")
    _timestamp(timer["anchorAt"], "canonicalTimer.anchorAt")
    for key in ("taskId", "startedByDeviceId"):
        if key in timer:
            _nonempty_string(timer[key], f"canonicalTimer.{key}")
    if "lastIntent" in timer:
        intent = _exact_object(
            timer["lastIntent"],
            {"type", "commandId", "occurredAt"},
            label="canonicalTimer.lastIntent",
        )
        _nonempty_string(intent["type"], "canonicalTimer.lastIntent.type")
        _nonempty_string(intent["commandId"], "canonicalTimer.lastIntent.commandId")
        _timestamp(intent["occurredAt"], "canonicalTimer.lastIntent.occurredAt")
    return timer


def _validated_history(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _projection_error("history is not an array")
    ids: set[str] = set()
    timer_ids: set[str] = set()
    for index, raw_item in enumerate(value):
        label = f"history[{index}]"
        item = _exact_object(
            raw_item,
            {"id", "timerId", "phase", "status", "plannedDurationMs"},
            optional={"taskId", "commandId", "completedAt", "endedAt"},
            label=label,
        )
        item_id = _nonempty_string(item["id"], f"{label}.id")
        timer_id = _nonempty_string(item["timerId"], f"{label}.timerId")
        if item_id in ids or timer_id in timer_ids:
            raise _projection_error("history identities are not unique")
        ids.add(item_id)
        timer_ids.add(timer_id)
        if item["phase"] not in _PHASES or item["status"] not in _TERMINAL_STATUSES:
            raise _projection_error(f"{label} phase or status is invalid")
        _duration(item["plannedDurationMs"], f"{label}.plannedDurationMs")
        for key in ("taskId", "commandId"):
            if key in item:
                _nonempty_string(item[key], f"{label}.{key}")
        for key in ("completedAt", "endedAt"):
            if key in item:
                _timestamp(item[key], f"{label}.{key}")
    return value


def _validated_tasks(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _projection_error("tasks is not an array")
    ids: set[str] = set()
    for index, raw_task in enumerate(value):
        label = f"tasks[{index}]"
        task = _exact_object(raw_task, {"id", "title"}, label=label)
        task_id = _nonempty_string(task["id"], f"{label}.id")
        title = _nonempty_string(task["title"], f"{label}.title")
        if task_id in ids or len(title.encode("utf-8")) > 512:
            raise _projection_error("tasks contain invalid identity or title")
        ids.add(task_id)
    return value


def _validated_durations(value: object) -> dict[str, int]:
    durations = _exact_object(value, _PHASES, label="durationsMs")
    return {
        phase: _duration(durations[phase], f"durationsMs.{phase}")
        for phase in sorted(_PHASES)
    }


def _input_operations(input_value: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(input_value, dict) or not isinstance(
        input_value.get("pending"), dict
    ):
        raise _projection_error("adapter input has no pending object")
    pending = input_value["pending"]
    keys = {
        "commands",
        "taskOperations",
        "durationOperations",
        "autoStartOperations",
        "selectedTaskOperations",
    }
    if set(pending) != keys:
        raise _projection_error("adapter input pending fields are invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    for key in keys:
        operations = pending[key]
        if not isinstance(operations, list) or not all(
            isinstance(operation, dict) for operation in operations
        ):
            raise _projection_error(f"adapter input pending.{key} is invalid")
        result[key] = operations
    return result


def _operation_winner(
    operations: list[dict[str, Any]], label: str
) -> dict[str, Any] | None:
    if not operations:
        return None
    keys: list[tuple[int, int, str, str]] = []
    for operation in operations:
        keys.append(
            (
                _integer(operation.get("hlcWallMs"), f"input {label}.hlcWallMs"),
                _integer(operation.get("hlcCounter"), f"input {label}.hlcCounter"),
                _nonempty_string(operation.get("deviceId"), f"input {label}.deviceId"),
                _nonempty_string(operation.get("id"), f"input {label}.id"),
            )
        )
    return operations[max(range(len(operations)), key=keys.__getitem__)]


def _group_operations(
    operations: list[dict[str, Any]], field: str, label: str
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for operation in operations:
        key = _nonempty_string(operation.get(field), f"input {label}.{field}")
        groups.setdefault(key, []).append(operation)
    return groups


def _validated_outcomes(
    value: object, commands: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise _projection_error("timerOutcomes is not an object")
    command_ids = [
        _nonempty_string(command.get("id"), "input command.id") for command in commands
    ]
    if len(set(command_ids)) != len(command_ids) or set(value) != set(command_ids):
        raise _projection_error("timerOutcomes keys do not match pending commands")
    for command_id, raw_outcome in value.items():
        outcome = _exact_object(
            raw_outcome,
            {"outcome", "reason"},
            label=f"timerOutcomes.{command_id}",
        )
        if outcome["outcome"] not in _OUTCOMES or not isinstance(
            outcome["reason"], str
        ):
            raise _projection_error(f"timerOutcomes.{command_id} is invalid")
    return value


def _validated_task_winners(
    value: object,
    groups: dict[str, list[dict[str, Any]]],
    projected_tasks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    winners = _exact_object(value, set(groups), label="winningOperationIds.tasks")
    for task_id, group in groups.items():
        winner = _operation_winner(group, "task operation")
        assert winner is not None
        winner_id = _nonempty_string(
            winners[task_id], f"winningOperationIds.tasks.{task_id}"
        )
        expected = (
            {"id": task_id, "title": winner.get("title")}
            if winner.get("type") == "upsert"
            else None
        )
        if winner_id != winner["id"] or projected_tasks.get(task_id) != expected:
            raise _projection_error("task winner is inconsistent")
    return winners


def _validated_duration_winners(
    value: object,
    groups: dict[str, list[dict[str, Any]]],
    durations: dict[str, int],
) -> dict[str, Any]:
    winners = _exact_object(value, set(groups), label="winningOperationIds.durations")
    for phase, group in groups.items():
        winner = _operation_winner(group, "duration operation")
        assert winner is not None
        winner_id = _nonempty_string(
            winners[phase], f"winningOperationIds.durations.{phase}"
        )
        if winner_id != winner["id"] or durations.get(phase) != winner.get(
            "durationMs"
        ):
            raise _projection_error("duration winner is inconsistent")
    return winners


def _validated_scalar_winner(
    winners: dict[str, Any],
    operations: dict[str, list[dict[str, Any]]],
    output_key: str,
    operation_key: str,
    output_value: object,
    value_key: str,
    projected_tasks: dict[str, dict[str, Any]],
) -> str | None:
    group = operations[operation_key]
    raw_winner_id = winners[output_key]
    if not group:
        if raw_winner_id is not None:
            raise _projection_error(f"{output_key} winner must be null")
        return None
    winner = _operation_winner(group, f"{output_key} operation")
    assert winner is not None
    winner_id = _nonempty_string(raw_winner_id, f"winningOperationIds.{output_key}")
    if winner_id != winner["id"]:
        raise _projection_error(f"{output_key} winner is inconsistent")
    expected = winner.get(value_key)
    if output_key == "selectedTask" and expected not in projected_tasks:
        expected = None
    if output_value != expected:
        raise _projection_error(f"{output_key} value is inconsistent")
    return winner_id


def _validated_winners(
    value: object,
    operations: dict[str, list[dict[str, Any]]],
    *,
    tasks: list[dict[str, Any]],
    durations: dict[str, int],
    auto_start: bool,
    selected_task_id: str | None,
) -> ProjectionWinningOperationIds:
    winners = _exact_object(
        value,
        {"tasks", "durations", "autoStart", "selectedTask"},
        label="winningOperationIds",
    )
    task_groups = _group_operations(operations["taskOperations"], "taskId", "task")
    duration_groups = _group_operations(
        operations["durationOperations"], "phase", "duration"
    )
    projected_tasks = {task["id"]: task for task in tasks}
    task_winners = _validated_task_winners(
        winners["tasks"], task_groups, projected_tasks
    )
    duration_winners = _validated_duration_winners(
        winners["durations"], duration_groups, durations
    )
    auto_winner = _validated_scalar_winner(
        winners,
        operations,
        "autoStart",
        "autoStartOperations",
        auto_start,
        "enabled",
        projected_tasks,
    )
    selected_winner = _validated_scalar_winner(
        winners,
        operations,
        "selectedTask",
        "selectedTaskOperations",
        selected_task_id,
        "taskId",
        projected_tasks,
    )
    return ProjectionWinningOperationIds(
        tasks=dict(task_winners),
        durations=dict(duration_winners),
        auto_start=auto_winner,
        selected_task=selected_winner,
    )


def _validated_projection(value: object, input_value: object) -> ProjectionApplyV2:
    projection = _exact_object(value, _PROJECTION_KEYS, label="projection")
    timer = _validated_timer(projection["canonicalTimer"])
    history = _validated_history(projection["history"])
    tasks = _validated_tasks(projection["tasks"])
    durations = _validated_durations(projection["durationsMs"])
    auto_start = projection["autoStartBreaks"]
    if not isinstance(auto_start, bool):
        raise _projection_error("autoStartBreaks is not boolean")
    selected_task_id = projection["selectedTaskId"]
    if selected_task_id is not None:
        selected_task_id = _nonempty_string(selected_task_id, "selectedTaskId")
        if not any(task["id"] == selected_task_id for task in tasks):
            raise _projection_error("selectedTaskId is unavailable")
    operations = _input_operations(input_value)
    outcomes = _validated_outcomes(projection["timerOutcomes"], operations["commands"])
    winning_ids = _validated_winners(
        projection["winningOperationIds"],
        operations,
        tasks=tasks,
        durations=durations,
        auto_start=auto_start,
        selected_task_id=selected_task_id,
    )
    return ProjectionApplyV2(
        canonical_timer=deepcopy(timer),
        history=deepcopy(history),
        tasks=deepcopy(tasks),
        durations_ms=durations,
        auto_start_breaks=auto_start,
        selected_task_id=selected_task_id,
        timer_outcomes=deepcopy(outcomes),
        winning_operation_ids=winning_ids,
    )


def _read_packaged_wasm() -> bytes:
    resources = files("pomodorough").joinpath("resources")
    try:
        commit = resources.joinpath("CORE_COMMIT").read_text(encoding="ascii").strip()
        checksum = resources.joinpath(f"{WASM_RESOURCE}.sha256").read_text(
            encoding="ascii"
        )
        wasm = resources.joinpath(WASM_RESOURCE).read_bytes()
    except (FileNotFoundError, OSError) as cause:
        raise SharedCoreLoadError(
            "packaged shared-core resources are missing"
        ) from cause

    if commit != CORE_COMMIT:
        raise SharedCoreLoadError(
            f"shared-core commit mismatch: expected {CORE_COMMIT}, got {commit}"
        )
    fields = checksum.split()
    if fields != [CORE_SHA256, WASM_RESOURCE]:
        raise SharedCoreLoadError("packaged shared-core checksum manifest is invalid")
    return wasm


def _signed_i32(value: int) -> int:
    return value if value <= 0x7FFF_FFFF else value - (1 << 32)


__all__ = [
    "CORE_COMMIT",
    "CORE_SHA256",
    "WASM_RESOURCE",
    "ProjectionApplyV2",
    "ProjectionWinningOperationIds",
    "SharedCore",
    "SharedCoreABIError",
    "SharedCoreDispatcher",
    "SharedCoreError",
    "SharedCoreLoadError",
    "SharedCoreOperationError",
    "apply_projection_v2",
]
