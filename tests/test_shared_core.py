from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from importlib.resources import files
import unittest

from wasmtime import WasmtimeError

from pomodorough.shared_core import (
    CORE_COMMIT,
    CORE_SHA256,
    SharedCore,
    SharedCoreABIError,
    SharedCoreLoadError,
    SharedCoreOperationError,
)


SELECTED_TASK_ID = "33f9d32c-a7ee-8aa9-897a-13e19bc4e5d4"


class SharedCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core = SharedCore()

    def test_core_version_through_wasm_abi(self) -> None:
        self.assertEqual(version("wasmtime"), "48.0.0")
        self.assertEqual(
            self.core.dispatch("core.version", {}),
            {"schemaVersion": 1, "coreVersion": "0.1.0"},
        )

    def test_selected_task_classification_through_wasm_abi(self) -> None:
        cases = (
            ({}, "omitted"),
            ({"selectedTaskId": None}, "deselected"),
            (
                {"selectedTaskId": SELECTED_TASK_ID},
                f"selected:{SELECTED_TASK_ID}",
            ),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.core.dispatch("selectedTask.classify", payload), expected
                )

    def test_one_instance_safely_serves_concurrent_dispatches(self) -> None:
        def classify(index: int) -> object:
            return self.core.dispatch(
                "selectedTask.classify", {"selectedTaskId": f"task-{index}"}
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(classify, range(32)))

        self.assertEqual(results, [f"selected:task-{index}" for index in range(32)])

    def test_core_errors_become_typed_host_errors(self) -> None:
        with self.assertRaises(SharedCoreOperationError) as raised:
            self.core.dispatch("missing.operation", {})

        self.assertEqual(raised.exception.operation, "missing.operation")
        self.assertEqual(
            str(raised.exception),
            "shared-core operation missing.operation failed: "
            "unsupported shared-core operation: missing.operation",
        )

    def test_oversized_result_is_still_released(self) -> None:
        core = SharedCore()
        released: list[tuple[int, int]] = []
        pointers = iter((100, 200))
        core._allocate = lambda _value: next(pointers)
        core._dispatch_export = lambda *_args: (16_777_217 << 32) | 300
        core._release = lambda pointer, length: released.append((pointer, length))

        with self.assertRaisesRegex(SharedCoreABIError, "result is too large"):
            core._dispatch_locked("core.version", b"v", b"{}")

        self.assertEqual(released, [(300, 16_777_217), (200, 2), (100, 1)])

    def test_cleanup_failure_preserves_primary_and_invalidates_instance(self) -> None:
        core = SharedCore()

        def fail_release(pointer: int, length: int) -> None:
            raise RuntimeError(f"free failed {pointer}/{length}")

        core._release = fail_release
        with self.assertRaises(SharedCoreOperationError) as raised:
            core.dispatch("missing.operation", {})
        self.assertTrue(raised.exception.__notes__)

        with self.assertRaisesRegex(SharedCoreABIError, "unusable after cleanup failure"):
            core.dispatch("core.version", {})

    def test_rejects_noncanonical_result_envelopes(self) -> None:
        invalid = (
            '{"ok":true,"value":{},"extra":true}',
            '{"ok":true,"value":{},"error":"bad"}',
            '{"ok":false,"error":"bad","value":{}}',
            '{"ok":false,"error":""}',
            '{"ok":false,"error":7}',
            '{"ok":true,"value":NaN}',
            '{"ok":false,"ok":true,"value":1}',
        )
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(
                SharedCoreABIError
            ):
                SharedCore._parse_envelope("core.version", document)

    def test_rejects_empty_input_before_entering_abi(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.core.dispatch_json("core.version", "")

    def test_module_hash_is_verified_before_instantiation(self) -> None:
        resource = files("pomodorough").joinpath(
            "resources/pomodorough_core.wasm"
        )
        modified = bytearray(resource.read_bytes())
        modified[-1] ^= 1

        with self.assertRaisesRegex(
            SharedCoreLoadError, "shared-core SHA-256 mismatch"
        ):
            SharedCore.load(modified)

    def test_packaged_pin_metadata_matches_adapter(self) -> None:
        resources = files("pomodorough").joinpath("resources")

        self.assertEqual(
            resources.joinpath("CORE_COMMIT").read_text(encoding="ascii").strip(),
            CORE_COMMIT,
        )
        self.assertEqual(
            resources.joinpath("pomodorough_core.wasm.sha256")
            .read_text(encoding="ascii")
            .split(),
            [CORE_SHA256, "pomodorough_core.wasm"],
        )

    def test_store_enforces_linear_memory_ceiling(self) -> None:
        core = SharedCore()
        with self.assertRaises(WasmtimeError):
            core._memory.grow(core._store, 4_096)


if __name__ == "__main__":
    unittest.main()
