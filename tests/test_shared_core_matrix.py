from __future__ import annotations

import unittest
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from test_shared_core import overdue_pause_resume_input

from pomodorough.shared_core import SharedCore, SharedCoreABIError, apply_projection_v2


class _ProjectionDispatcher:
    def __init__(self, output: object) -> None:
        self.output = output

    def dispatch(self, operation: str, input_value: object) -> object:
        if operation != "projection.apply.v2":
            raise AssertionError(operation)
        return self.output


class SharedCoreValidationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core = SharedCore()
        cls.input_value = overdue_pause_resume_input()
        cls.output = cls.core.dispatch("projection.apply.v2", cls.input_value)

    def assert_rejected(
        self,
        mutate_output: Callable[[dict[str, Any]], None],
        *,
        input_value: object | None = None,
    ) -> None:
        output = deepcopy(self.output)
        mutate_output(output)
        with self.assertRaises(SharedCoreABIError):
            apply_projection_v2(
                _ProjectionDispatcher(output),
                deepcopy(self.input_value) if input_value is None else input_value,
            )

    def test_timer_validation_rejects_each_semantically_invalid_field(self) -> None:
        def timer_mutation(key: str, value: object) -> Callable[[dict[str, Any]], None]:
            def mutate(output: dict[str, Any]) -> None:
                output["canonicalTimer"][key] = value

            return mutate

        scenarios = (
            timer_mutation("id", ""),
            timer_mutation("phase", "invalid"),
            timer_mutation("status", "invalid"),
            timer_mutation("plannedDurationMs", True),
            timer_mutation("plannedDurationMs", 59_999),
            timer_mutation("elapsedAtAnchorMs", -1),
            timer_mutation("anchorAt", "2026-01-01T00:00:00"),
            timer_mutation("taskId", ""),
            timer_mutation("lastIntent", []),
        )
        for index, mutate in enumerate(scenarios):
            with self.subTest(index=index):
                self.assert_rejected(mutate)

    def test_history_validation_rejects_shape_identity_state_and_time_errors(self) -> None:
        def history_mutation(
            mutate_item: Callable[[dict[str, Any]], None],
        ) -> Callable[[dict[str, Any]], None]:
            def mutate(output: dict[str, Any]) -> None:
                mutate_item(output["history"][0])

            return mutate

        scenarios = (
            lambda output: output.update(history={}),
            history_mutation(lambda item: item.update(id="")),
            history_mutation(lambda item: item.update(phase="invalid")),
            history_mutation(lambda item: item.update(status="running")),
            history_mutation(lambda item: item.update(plannedDurationMs=1)),
            history_mutation(lambda item: item.update(taskId="")),
            history_mutation(lambda item: item.update(endedAt="not-a-time")),
            lambda output: output["history"].append(deepcopy(output["history"][0])),
        )
        for index, mutate in enumerate(scenarios):
            with self.subTest(index=index):
                self.assert_rejected(mutate)

    def test_task_duration_and_selection_validation_rejects_bad_projection(self) -> None:
        scenarios = (
            lambda output: output.update(tasks={}),
            lambda output: output.update(tasks=[{"id": "task-1", "title": ""}]),
            lambda output: output.update(
                tasks=[
                    {"id": "task-1", "title": "First"},
                    {"id": "task-1", "title": "Second"},
                ]
            ),
            lambda output: output.update(
                tasks=[{"id": "task-1", "title": "😀" * 129}]
            ),
            lambda output: output.update(durationsMs={}),
            lambda output: output["durationsMs"].update(focus=59_999),
            lambda output: output.update(autoStartBreaks=1),
            lambda output: output.update(selectedTaskId="missing-task"),
        )
        for index, mutate in enumerate(scenarios):
            with self.subTest(index=index):
                self.assert_rejected(mutate)

    def test_adapter_input_rejects_missing_extra_and_non_object_operations(self) -> None:
        inputs = (
            None,
            {},
            {**deepcopy(self.input_value), "pending": {}},
            {
                **deepcopy(self.input_value),
                "pending": {
                    **deepcopy(self.input_value["pending"]),
                    "commands": ["not-an-object"],
                },
            },
        )
        for input_value in inputs:
            with self.subTest(input_value=input_value), self.assertRaises(
                SharedCoreABIError
            ):
                apply_projection_v2(_ProjectionDispatcher(self.output), input_value)

    def test_outcomes_and_empty_operation_winners_fail_closed(self) -> None:
        scenarios = (
            lambda output: output.update(timerOutcomes=[]),
            lambda output: output["timerOutcomes"]["command-1"].update(
                outcome="unknown"
            ),
            lambda output: output["timerOutcomes"]["command-1"].update(reason=1),
            lambda output: output["winningOperationIds"].update(autoStart="unexpected"),
            lambda output: output["winningOperationIds"].update(
                selectedTask="unexpected"
            ),
            lambda output: output["winningOperationIds"].update(tasks=[]),
        )
        for index, mutate in enumerate(scenarios):
            with self.subTest(index=index):
                self.assert_rejected(mutate)

    def test_dispatch_input_guards_reject_types_sizes_and_non_json_values(self) -> None:
        cases = (
            lambda: self.core.dispatch_json(1, "{}"),
            lambda: self.core.dispatch_json("core.version", 1),
            lambda: self.core.dispatch_json("", "{}"),
            lambda: self.core.dispatch_json("x" * 257, "{}"),
            lambda: self.core.dispatch("core.version", {"bad": object()}),
            lambda: self.core.dispatch("core.version", {"bad": float("nan")}),
        )
        for index, invoke in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((TypeError, ValueError)):
                invoke()


if __name__ == "__main__":
    unittest.main()
