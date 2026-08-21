"""Thread-safe host adapter for the pinned shared-core WebAssembly ABI."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from threading import Lock
from typing import Final, Self

from wasmtime import Engine, Func, Instance, Memory, Module, Store, WasmtimeError


CORE_COMMIT: Final = "a78a312314dd9466557c3dbdd12184b698c3d156"
CORE_SHA256: Final = "89fb6300324042b61d62070242cccad10e30f125885bb1b7a05af67b077bac83"
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


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


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
        self._free_export = self._require_func(
            "pomodorough_free", ("i32", "i32"), ()
        )
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
        operation_pointer = 0
        input_pointer = 0
        result_pointer = 0
        result_length = 0
        result_owned = False
        value: object = None
        primary: BaseException | None = None

        try:
            operation_pointer = self._allocate(operation_bytes)
            input_pointer = self._allocate(input_bytes)
            packed_result = self._dispatch_export(
                self._store,
                _signed_i32(operation_pointer),
                len(operation_bytes),
                _signed_i32(input_pointer),
                len(input_bytes),
            )
            if not isinstance(packed_result, int):
                raise SharedCoreABIError("pomodorough_dispatch did not return an i64")

            packed_bits = packed_result & _UINT64_MASK
            result_pointer = packed_bits & _UINT32_MASK
            result_length = packed_bits >> 32
            result_owned = result_pointer != 0 and result_length != 0
            if result_length > _MAX_OUTPUT_BYTES:
                raise SharedCoreABIError(
                    f"dispatch result is too large: {result_length} bytes"
                )
            self._require_range(result_pointer, result_length, "dispatch result")
            result_bytes = bytes(
                self._memory.read(
                    self._store,
                    result_pointer,
                    result_pointer + result_length,
                )
            )
            try:
                envelope_json = result_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as cause:
                raise SharedCoreABIError("dispatch result is not UTF-8") from cause
            value = self._parse_envelope(operation, envelope_json)
        except BaseException as cause:
            if isinstance(cause, SharedCoreError):
                primary = cause
            elif isinstance(cause, (IndexError, WasmtimeError)):
                primary = SharedCoreABIError("shared-core ABI call failed")
                primary.__cause__ = cause
            else:
                primary = cause

        cleanup_errors: list[BaseException] = []
        for pointer, length in (
            (result_pointer, result_length) if result_owned else (0, 0),
            (input_pointer, len(input_bytes)),
            (operation_pointer, len(operation_bytes)),
        ):
            try:
                self._release(pointer, length)
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)

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
        if pointer == 0 or length == 0:
            return
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


def _read_packaged_wasm() -> bytes:
    resources = files("pomodorough").joinpath("resources")
    try:
        commit = resources.joinpath("CORE_COMMIT").read_text(encoding="ascii").strip()
        checksum = resources.joinpath(f"{WASM_RESOURCE}.sha256").read_text(
            encoding="ascii"
        )
        wasm = resources.joinpath(WASM_RESOURCE).read_bytes()
    except (FileNotFoundError, OSError) as cause:
        raise SharedCoreLoadError("packaged shared-core resources are missing") from cause

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
    "SharedCore",
    "SharedCoreABIError",
    "SharedCoreError",
    "SharedCoreLoadError",
    "SharedCoreOperationError",
    "WASM_RESOURCE",
]
