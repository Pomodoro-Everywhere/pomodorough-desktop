from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from importlib.resources import files

from wasmtime import WasmtimeError

from pomodorough.core import parse_timestamp_ms, rebuild_optimistic
from pomodorough.shared_core import (
    CORE_COMMIT,
    CORE_SHA256,
    ProjectionApplyV2,
    SharedCore,
    SharedCoreABIError,
    SharedCoreLoadError,
    SharedCoreOperationError,
    apply_projection_v2,
)

SELECTED_TASK_ID = "33f9d32c-a7ee-8aa9-897a-13e19bc4e5d4"


def overdue_pause_resume_input() -> dict[str, object]:
    commands = []
    for sequence, (command_type, occurred_at, wall_ms, observed_ms) in enumerate(
        (
            ("start", "1970-01-01T00:00:01.000Z", 1_000, 0),
            ("pause", "1970-01-01T00:01:10.000Z", 70_000, 60_000),
            ("resume", "1970-01-01T00:01:20.000Z", 80_000, 60_000),
        ),
        start=1,
    ):
        commands.append(
            {
                "id": f"command-{sequence}",
                "deviceId": "device-a",
                "deviceSequence": sequence,
                "timerId": "timer-overdue",
                "type": command_type,
                "phase": "focus",
                "plannedDurationMs": 60_000,
                "occurredAt": occurred_at,
                "hlcWallMs": wall_ms,
                "hlcCounter": 0,
                "observedElapsedMs": observed_ms,
            }
        )
    return {
        "base": {
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 60_000,
                "short_break": 300_000,
                "long_break": 900_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
        },
        "pending": {
            "commands": commands,
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        },
        "now": "1970-01-01T00:01:30.000Z",
    }


class SharedCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core = SharedCore()

    def test_core_version_through_wasm_abi(self) -> None:
        self.assertEqual(version("wasmtime"), "48.0.0")
        self.assertEqual(
            self.core.dispatch("core.version", {}),
            {"schemaVersion": 1, "coreVersion": "0.1.2"},
        )

    def test_typed_projection_overdue_pause_then_resume_keeps_history_identity(
        self,
    ) -> None:
        projection = self.core.apply_projection_v2(overdue_pause_resume_input())

        self.assertIsInstance(projection, ProjectionApplyV2)
        self.assertEqual(projection.canonical_timer["status"], "completed")
        self.assertEqual(
            projection.canonical_timer["lastIntent"],
            {
                "type": "resume",
                "commandId": "command-3",
                "occurredAt": "1970-01-01T00:01:20Z",
            },
        )
        self.assertEqual(len(projection.history), 1)
        self.assertEqual(
            (
                projection.history[0]["id"],
                projection.history[0]["timerId"],
            ),
            ("timer-overdue", "timer-overdue"),
        )
        self.assertTrue(
            all(
                outcome["outcome"] == "applied"
                for outcome in projection.timer_outcomes.values()
            )
        )

    def test_typed_projection_fails_closed_on_malformed_output(self) -> None:
        class MalformedProjection:
            @staticmethod
            def dispatch(operation: str, input_value: object) -> object:
                del input_value
                self.assertEqual(operation, "projection.apply.v2")
                return {"canonicalTimer": None}

        with self.assertRaisesRegex(
            SharedCoreABIError, "malformed projection.apply.v2 output"
        ):
            apply_projection_v2(MalformedProjection(), overdue_pause_resume_input())

    def test_typed_projection_fails_closed_on_mismatched_outcomes(self) -> None:
        output = self.core.dispatch(
            "projection.apply.v2", overdue_pause_resume_input()
        )
        del output["timerOutcomes"]["command-3"]

        class MismatchedProjection:
            @staticmethod
            def dispatch(operation: str, input_value: object) -> object:
                del input_value
                self.assertEqual(operation, "projection.apply.v2")
                return output

        with self.assertRaisesRegex(
            SharedCoreABIError, "timerOutcomes keys do not match pending commands"
        ):
            apply_projection_v2(MismatchedProjection(), overdue_pause_resume_input())

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

    def test_history_identity_replay_matches_bundled_core(self) -> None:
        historical = {
            "id": "history-custom",
            "timerId": "timer-a",
            "commandId": "old-finish",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 1_500_000,
            "completedAt": "2026-07-20T11:59:00.000Z",
            "endedAt": "2026-07-20T11:59:00.000Z",
        }

        def command(
            command_id: str,
            command_type: str,
            timer_id: str,
            wall_ms: int,
            occurred_at: str,
            observed_ms: int,
        ) -> dict[str, object]:
            return {
                "id": command_id,
                "deviceId": "device-a",
                "deviceSequence": wall_ms,
                "timerId": timer_id,
                "type": command_type,
                "phase": "focus",
                "plannedDurationMs": 1_500_000,
                "occurredAt": occurred_at,
                "hlcWallMs": wall_ms,
                "hlcCounter": 0,
                "observedElapsedMs": observed_ms,
            }

        commands = [
            command(
                "command-pause",
                "pause",
                "timer-a",
                1_000,
                "2026-07-20T12:00:00.000Z",
                123_000,
            ),
            command(
                "command-start",
                "start",
                "timer-b",
                2_000,
                "2026-07-20T12:00:01.000Z",
                0,
            ),
        ]

        _native_timer, native_history = rebuild_optimistic(
            None, [historical], commands
        )
        core_projection = self.core.dispatch(
            "timer.reduce.v1",
            {
                "canonicalTimer": None,
                "history": [historical],
                "commands": commands,
                "now": "2026-07-20T12:00:02.000Z",
            },
        )

        def retained_contract(items: list[dict[str, object]]) -> list[tuple[object, ...]]:
            return [
                (
                    item["id"],
                    item["timerId"],
                    item.get("taskId"),
                    item.get("commandId"),
                    item["phase"],
                    item["status"],
                    item["plannedDurationMs"],
                    parse_timestamp_ms(str(item["endedAt"])),
                    parse_timestamp_ms(str(item["completedAt"]))
                    if item.get("completedAt")
                    else None,
                )
                for item in items
            ]

        self.assertEqual(
            retained_contract(native_history),
            retained_contract(core_projection["history"]),
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

    def test_null_result_pointer_with_nonzero_length_invalidates_instance(self) -> None:
        core = SharedCore()
        released: list[tuple[int, int]] = []
        pointers = iter((100, 200))
        core._allocate = lambda _value: next(pointers)
        core._dispatch_export = lambda *_args: 7 << 32
        core._free_export = (
            lambda _store, pointer, length: released.append((pointer, length))
        )

        with self.assertRaisesRegex(
            SharedCoreABIError, "null pointer with nonzero length"
        ):
            core._dispatch_locked("core.version", b"v", b"{}")

        self.assertEqual(released, [(200, 2), (100, 1)])
        with self.assertRaisesRegex(SharedCoreABIError, "unusable"):
            core.dispatch("core.version", {})

    def test_nonnull_result_pointer_with_zero_length_invalidates_instance(self) -> None:
        core = SharedCore()
        released: list[tuple[int, int]] = []
        pointers = iter((100, 200))
        core._allocate = lambda _value: next(pointers)
        core._dispatch_export = lambda *_args: 300
        core._free_export = (
            lambda _store, pointer, length: released.append((pointer, length))
        )

        with self.assertRaisesRegex(SharedCoreABIError, "empty result buffer"):
            core._dispatch_locked("core.version", b"v", b"{}")

        self.assertEqual(released, [(200, 2), (100, 1)])
        with self.assertRaisesRegex(SharedCoreABIError, "unusable"):
            core.dispatch("core.version", {})

    def test_zero_zero_result_is_empty_without_unsafe_ownership(self) -> None:
        core = SharedCore()
        released: list[tuple[int, int]] = []
        pointers = iter((100, 200))
        core._allocate = lambda _value: next(pointers)
        core._dispatch_export = lambda *_args: 0
        core._free_export = (
            lambda _store, pointer, length: released.append((pointer, length))
        )

        with self.assertRaisesRegex(SharedCoreABIError, "empty result buffer"):
            core._dispatch_locked("core.version", b"v", b"{}")

        self.assertEqual(released, [(200, 2), (100, 1)])
        self.assertIsNone(core._unusable_cause)

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
