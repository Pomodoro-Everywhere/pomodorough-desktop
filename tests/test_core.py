from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path

from pomodorough.core import (
    PHASES,
    elapsed_ms,
    format_remaining,
    next_break_phase,
    normalize_task_title,
    project_auto_start_breaks,
    project_durations,
    rebuild_optimistic,
    rebuild_tasks,
    reduce_command,
    task_from_title,
    task_id_for_title,
    task_summaries_today,
)


def command(
    kind: str,
    sequence: int = 1,
    observed: int = 0,
    task_id: str | None = None,
    timer_id: str = "timer-00000001",
) -> dict:
    result = {
        "id": f"command-{sequence:08d}",
        "deviceSequence": sequence,
        "timerId": timer_id,
        "type": kind,
        "phase": "focus",
        "plannedDurationMs": 1_500_000,
        "occurredAt": "2026-07-20T12:00:00.000Z",
        "hlcWallMs": 1_000,
        "hlcCounter": 0,
        "observedElapsedMs": observed,
    }
    if task_id:
        result["taskId"] = task_id
    return result


class CoreTests(unittest.TestCase):
    def test_portable_canonical_convergence_fixture(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "convergence-v1.json"
        fixture_bytes = fixture_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(fixture_bytes).hexdigest(),
            "a293a679179f7f441a89b04f0260ee77fc0d810abc61e99501f9260a6ea9012e",
        )
        fixture = json.loads(fixture_bytes)
        self.assertEqual(fixture["version"], 2)
        epoch = datetime.fromisoformat(fixture["epoch"].replace("Z", "+00:00"))

        for scenario in fixture["cases"]:
            commands = [
                {
                    "id": item["id"],
                    "deviceSequence": item["sequence"],
                    "timerId": item["timerId"],
                    "taskId": item.get("taskId"),
                    "type": item["type"],
                    "phase": item["phase"],
                    "plannedDurationMs": item["durationMs"],
                    "occurredAt": (epoch + timedelta(milliseconds=item["atMs"]))
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "hlcWallMs": item["wallMs"],
                    "hlcCounter": item["counter"],
                    "observedElapsedMs": item["elapsedMs"],
                }
                for item in scenario["commands"]
            ]
            for arrival_order in (commands, list(reversed(commands))):
                with self.subTest(name=scenario["name"], order=arrival_order[0]["id"]):
                    timer, history = rebuild_optimistic(None, [], arrival_order)
                    self.assertEqual(
                        self._normalize_fixture_projection(timer, history, epoch),
                        scenario["expected"],
                    )

        for scenario in fixture["projectionCases"]:
            with self.subTest(name=scenario["name"], domain="tasks"):
                for arrival_order in permutations(scenario["taskOperations"]):
                    self.assertEqual(
                        rebuild_tasks([], list(arrival_order)),
                        scenario["expected"]["tasks"],
                    )
            with self.subTest(name=scenario["name"], domain="durations"):
                defaults = {
                    phase: definition["default_minutes"] * 60_000
                    for phase, definition in PHASES.items()
                }
                for arrival_order in permutations(scenario["durationOperations"]):
                    self.assertEqual(
                        project_durations(defaults, list(arrival_order)),
                        scenario["expected"]["durationsMs"],
                    )
            with self.subTest(name=scenario["name"], domain="auto-start"):
                for arrival_order in permutations(scenario["autoStartOperations"]):
                    self.assertEqual(
                        project_auto_start_breaks(False, list(arrival_order)),
                        scenario["expected"]["autoStartBreaks"],
                    )

        for scenario in fixture["responseCases"]:
            local = deepcopy(scenario["local"])
            for command_item in local["commands"]:
                command_item["deviceSequence"] = command_item.pop("sequence")
                command_item["plannedDurationMs"] = command_item.pop("durationMs")
                command_item["occurredAt"] = (
                    epoch + timedelta(milliseconds=command_item.pop("atMs"))
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                command_item["hlcWallMs"] = command_item.pop("wallMs")
                command_item["hlcCounter"] = command_item.pop("counter")
                command_item["observedElapsedMs"] = command_item.pop("elapsedMs")
            for key in (
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
            ):
                for operation in local[key]:
                    operation["occurredAt"] = (
                        epoch + timedelta(milliseconds=operation.pop("atMs"))
                    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    operation["hlcWallMs"] = operation.pop("wallMs")
                    operation["hlcCounter"] = operation.pop("counter")

            valid_outcomes = {"applied", "ignored", "rejected"}
            retained: dict[str, list[dict]] = {}
            for key in (
                "commands",
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
            ):
                acknowledgements = scenario["acknowledgements"][key]
                self.assertEqual(
                    {item["id"] for item in acknowledgements},
                    set(scenario["sentIds"][key]),
                )
                self.assertTrue(
                    all(
                        item["outcome"] in valid_outcomes
                        and isinstance(item["reason"], str)
                        for item in acknowledgements
                    )
                )
                acknowledged_ids = {item["id"] for item in acknowledgements}
                retained[key] = [
                    item for item in local[key] if item["id"] not in acknowledged_ids
                ]

            expected = scenario["expected"]
            self.assertEqual(
                [item["id"] for item in retained["commands"]],
                expected["commandIds"],
            )
            self.assertEqual(
                [item["id"] for item in retained["taskOperations"]],
                expected["taskOperationIds"],
            )
            self.assertEqual(
                [item["id"] for item in retained["durationOperations"]],
                expected["durationOperationIds"],
            )
            self.assertEqual(
                [item["id"] for item in retained["autoStartOperations"]],
                expected["autoStartOperationIds"],
            )

            canonical = scenario["canonical"]
            canonical_timer = deepcopy(canonical["timer"])
            canonical_timer["plannedDurationMs"] = canonical_timer.pop("durationMs")
            canonical_timer["elapsedAtAnchorMs"] = canonical_timer.pop("elapsedMs")
            canonical_timer["anchorAt"] = (
                epoch + timedelta(milliseconds=canonical_timer.pop("anchorMs"))
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            canonical_timer["lastIntent"] = {
                "type": "start",
                "commandId": canonical_timer.pop("lastCommandId"),
                "occurredAt": canonical_timer["anchorAt"],
                "deviceId": "device-a",
            }
            timer, history = rebuild_optimistic(
                canonical_timer,
                [],
                retained["commands"],
            )
            self.assertEqual(
                self._normalize_fixture_projection(timer, history, epoch),
                {"timer": expected["timer"], "history": expected["history"]},
            )
            self.assertEqual(
                rebuild_tasks(canonical["tasks"], retained["taskOperations"]),
                expected["tasks"],
            )
            self.assertEqual(
                project_durations(
                    canonical["durationsMs"], retained["durationOperations"]
                ),
                expected["durationsMs"],
            )
            self.assertEqual(
                project_auto_start_breaks(
                    canonical["autoStartBreaks"],
                    retained["autoStartOperations"],
                ),
                expected["autoStartBreaks"],
            )

    @staticmethod
    def _normalize_fixture_projection(
        timer: dict | None, history: list[dict], epoch: datetime
    ) -> dict:
        result = {
            "history": [
                {
                    key: value
                    for key, value in {
                        "timerId": item["timerId"],
                        "status": item["status"],
                        "phase": item["phase"],
                        "durationMs": item["plannedDurationMs"],
                        "commandId": item.get("commandId"),
                        "endedMs": int(
                            (
                                datetime.fromisoformat(
                                    item["endedAt"].replace("Z", "+00:00")
                                )
                                - epoch
                            ).total_seconds()
                            * 1000
                        ),
                        "taskId": item.get("taskId"),
                    }.items()
                    if value is not None
                }
                for item in history
            ]
        }
        if timer is not None:
            result["timer"] = {
                key: value
                for key, value in {
                    "id": timer["id"],
                    "status": timer["status"],
                    "phase": timer["phase"],
                    "durationMs": timer["plannedDurationMs"],
                    "elapsedMs": timer["elapsedAtAnchorMs"],
                    "anchorMs": int(
                        (
                            datetime.fromisoformat(
                                timer["anchorAt"].replace("Z", "+00:00")
                            )
                            - epoch
                        ).total_seconds()
                        * 1000
                    ),
                    "lastCommandId": timer.get("lastIntent", {}).get("commandId", ""),
                    "taskId": timer.get("taskId"),
                }.items()
                if value is not None
            }
        return result

    def test_timer_state_command_target_matrix(self) -> None:
        states = (
            "absent",
            "running",
            "paused",
            "completed",
            "cancelled",
            "superseded",
        )
        commands = ("start", "pause", "resume", "finish", "cancel", "clear")
        cases = 0

        for state in states:
            for command_type in commands:
                for target in ("same", "foreign"):
                    with self.subTest(
                        state=state, command=command_type, target=target
                    ):
                        setup = [command("start", 1)] if state != "absent" else []
                        if state == "paused":
                            setup.append(command("pause", 2, 1_000))
                        elif state == "completed":
                            setup.append(command("finish", 2, 1_000))
                        elif state == "cancelled":
                            setup.append(command("cancel", 2, 1_000))
                        elif state == "superseded":
                            setup.append(
                                command("start", 2, timer_id="timer-current")
                            )
                        timer, history = rebuild_optimistic(None, [], setup)
                        setup_timer = deepcopy(timer)
                        setup_history = deepcopy(history)
                        target_id = (
                            "timer-00000001" if target == "same" else "timer-foreign"
                        )
                        action = command(
                            command_type,
                            sequence=99,
                            observed=123_000,
                            timer_id=target_id,
                        )
                        timer, history = reduce_command(timer, history, action)

                        start_applies = command_type == "start" and (
                            target == "foreign" or state == "absent"
                        )
                        action_applies = start_applies or target == "same" and (
                            (command_type == "pause" and state == "running")
                            or command_type == "resume"
                            and state in {"paused", "superseded"}
                            or command_type in {"finish", "cancel"}
                            and state in {"running", "paused"}
                            or command_type == "clear"
                            and state in {"completed", "cancelled"}
                        )
                        expected_id = None if state == "absent" else "timer-00000001"
                        expected_status = None if state == "absent" else state
                        if state == "superseded":
                            expected_id, expected_status = "timer-current", "running"
                        if start_applies:
                            expected_id, expected_status = target_id, "running"
                        elif target == "same":
                            transition = {
                                ("running", "pause"): "paused",
                                ("paused", "resume"): "running",
                                ("superseded", "resume"): "running",
                                ("running", "finish"): "completed",
                                ("paused", "finish"): "completed",
                                ("running", "cancel"): "cancelled",
                                ("paused", "cancel"): "cancelled",
                            }.get((state, command_type))
                            if transition is not None:
                                expected_id, expected_status = (
                                    "timer-00000001",
                                    transition,
                                )
                            elif command_type == "clear" and state in {
                                "completed",
                                "cancelled",
                            }:
                                expected_id, expected_status = None, None

                        self.assertEqual(
                            (
                                timer.get("id") if timer else None,
                                timer.get("status") if timer else None,
                            ),
                            (expected_id, expected_status),
                        )
                        history_projection = [
                            (
                                item["timerId"],
                                item["status"],
                                item.get("commandId"),
                                item["phase"],
                                item["plannedDurationMs"],
                                item.get("taskId"),
                            )
                            for item in history
                        ]
                        expected_history = [
                            (
                                item["timerId"],
                                item["status"],
                                item.get("commandId"),
                                item["phase"],
                                item["plannedDurationMs"],
                                item.get("taskId"),
                            )
                            for item in setup_history
                        ]
                        terminal_status = None
                        terminal_timer_id = "timer-00000001"
                        if start_applies and state in {"running", "paused"}:
                            terminal_status = "superseded"
                        elif command_type in {"finish", "cancel"} and target == "same" and state in {"running", "paused"}:
                            terminal_status = (
                                "completed"
                                if command_type == "finish"
                                else "cancelled"
                            )
                        elif state == "superseded" and (
                            start_applies
                            or command_type == "resume" and target == "same"
                        ):
                            terminal_status = "superseded"
                            terminal_timer_id = "timer-current"
                        if terminal_status is not None:
                            expected_history.insert(
                                0,
                                (
                                    terminal_timer_id,
                                    terminal_status,
                                    action["id"],
                                    "focus",
                                    1_500_000,
                                    None,
                                ),
                            )
                        if command_type == "resume" and target == "same" and state == "superseded":
                            expected_history = [
                                item
                                for item in expected_history
                                if not (
                                    item[0] == "timer-00000001"
                                    and item[1] == "superseded"
                                )
                            ]
                        self.assertEqual(history_projection, expected_history)
                        if not action_applies:
                            self.assertEqual(timer, setup_timer)
                            self.assertEqual(history, setup_history)
                        elif timer is not None:
                            self.assertEqual(
                                timer["lastIntent"]["commandId"], action["id"]
                            )
                            if command_type == "pause":
                                self.assertEqual(timer["elapsedAtAnchorMs"], 123_000)
                            elif command_type == "resume":
                                self.assertEqual(timer["elapsedAtAnchorMs"], 123_000)
                            elif command_type == "finish":
                                self.assertEqual(timer["elapsedAtAnchorMs"], 1_500_000)
                            elif command_type == "cancel":
                                self.assertEqual(timer["elapsedAtAnchorMs"], 123_000)
                        cases += 1

        self.assertEqual(cases, 72)

    def test_optimistic_reducer_is_deterministic_for_every_arrival_order(self) -> None:
        scenarios = (
            [
                command("start", 1),
                command("pause", 2, 60_000),
                command("resume", 3, 90_000),
                command("finish", 4, 120_000),
            ],
            [
                command("start", 1),
                command("start", 2, timer_id="timer-foreign"),
                command("resume", 3, 90_000),
                command("finish", 4, 120_000),
                command("clear", 5),
            ],
            [
                command("start", 1),
                command("start", 2),
                command("cancel", 3, 120_000),
                command("clear", 4),
            ],
        )
        for commands in scenarios:
            expected = rebuild_optimistic(None, [], commands)
            for arrival_order in permutations(commands):
                self.assertEqual(
                    rebuild_optimistic(None, [], list(arrival_order)), expected
                )

    def test_late_commands_observe_server_auto_completion(self) -> None:
        start = command("start", 1)
        start["occurredAt"] = "2026-07-20T12:00:00.000Z"
        late_cancel = command("cancel", 2, observed=1)
        late_cancel["occurredAt"] = "2026-07-20T12:30:00.000Z"

        timer, history = rebuild_optimistic(None, [], [start, late_cancel])

        self.assertEqual(timer["status"], "completed")
        self.assertEqual(timer["anchorAt"], "2026-07-20T12:25:00.000Z")
        self.assertEqual(timer["lastIntent"]["commandId"], start["id"])
        self.assertEqual(
            history,
            [
                {
                    "id": start["timerId"],
                    "timerId": start["timerId"],
                    "phase": "focus",
                    "status": "completed",
                    "plannedDurationMs": 1_500_000,
                    "completedAt": "2026-07-20T12:25:00.000Z",
                    "endedAt": "2026-07-20T12:25:00.000Z",
                    "taskId": None,
                }
            ],
        )

        deadline_finish = command("finish", 2, observed=1_500_000)
        deadline_finish["occurredAt"] = "2026-07-20T12:25:00.000Z"
        claimed, claimed_history = rebuild_optimistic(
            None, [], [start, deadline_finish]
        )
        self.assertEqual(
            claimed["lastIntent"]["commandId"], deadline_finish["id"]
        )
        self.assertEqual(
            claimed_history[0]["commandId"], deadline_finish["id"]
        )
        self.assertTrue(claimed_history[0]["pending"])

    def test_timer_command_lifecycle(self) -> None:
        timer, history = reduce_command(None, [], command("start"))
        self.assertEqual(timer["status"], "running")
        timer, history = reduce_command(timer, history, command("pause", 2, 123_000))
        self.assertEqual(timer["elapsedAtAnchorMs"], 123_000)
        self.assertEqual(timer["status"], "paused")
        timer, history = reduce_command(timer, history, command("resume", 3, 123_000))
        self.assertEqual(timer["status"], "running")
        timer, history = reduce_command(timer, history, command("finish", 4, 200_000))
        self.assertEqual(timer["status"], "completed")
        self.assertEqual(len(history), 1)

    def test_completion_replay_deduplicates_and_keeps_newest_first(self) -> None:
        running, _history = reduce_command(None, [], command("start"))
        older = {
            "id": "older-completion",
            "timerId": "timer-older",
            "commandId": "command-older",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 1_500_000,
            "completedAt": "2026-07-20T11:00:00.000Z",
        }
        finish = command("finish", 2, 1_500_000)

        _completed, history = reduce_command(running, [older], finish)
        _replayed, replayed_history = reduce_command(running, history, finish)

        self.assertEqual(
            [item["id"] for item in history],
            ["timer-00000001:command-00000002", "older-completion"],
        )
        self.assertEqual(replayed_history, history)

    def test_elapsed_uses_positive_wall_time_and_clamps(self) -> None:
        timer = {
            "status": "running",
            "plannedDurationMs": 60_000,
            "elapsedAtAnchorMs": 10_000,
            "anchorAt": "1970-01-01T00:00:10.000Z",
        }
        self.assertEqual(elapsed_ms(timer, 5_000), 10_000)
        self.assertEqual(elapsed_ms(timer, 100_000), 60_000)

    def test_fourth_focus_uses_long_break(self) -> None:
        history = [{"phase": "focus", "status": "completed"} for _ in range(4)]
        self.assertEqual(next_break_phase(history), "long_break")
        self.assertEqual(next_break_phase(history[:3]), "short_break")

    def test_remaining_rounds_up(self) -> None:
        self.assertEqual(format_remaining(60_001), "01:01")
        self.assertEqual(format_remaining(0), "00:00")

    def test_task_identity_normalizes_unicode_and_printability(self) -> None:
        task = task_from_title("\x00Cafe\u0301\x1f")
        self.assertEqual(task["title"], "Café")
        self.assertEqual(task["id"], "aaf83054-24b2-8c0e-901f-a974147bfe82")
        self.assertEqual(normalize_task_title("\u00a0 spaced \u00a0"), " spaced ")

    def test_task_projection_uses_hlc_order_and_allows_recreation(self) -> None:
        task = task_from_title("Write release notes")
        operations = [
            {
                "id": "operation-3",
                "taskId": task["id"],
                "type": "upsert",
                "title": task["title"],
                "hlcWallMs": 20,
                "hlcCounter": 1,
            },
            {
                "id": "operation-1",
                "taskId": task["id"],
                "type": "upsert",
                "title": task["title"],
                "hlcWallMs": 10,
                "hlcCounter": 0,
            },
            {
                "id": "operation-2",
                "taskId": task["id"],
                "type": "delete",
                "hlcWallMs": 20,
                "hlcCounter": 0,
            },
        ]
        self.assertEqual(rebuild_tasks([], operations), [task])

    def test_task_lww_is_permutation_invariant_and_idempotent(self) -> None:
        retained = task_from_title("Retained task")
        deleted = task_from_title("Deleted task")
        operations = [
            {
                "id": "retained-upsert",
                "taskId": retained["id"],
                "type": "upsert",
                "title": retained["title"],
                "hlcWallMs": 10,
                "hlcCounter": 0,
            },
            {
                "id": "retained-delete",
                "taskId": retained["id"],
                "type": "delete",
                "hlcWallMs": 10,
                "hlcCounter": 1,
            },
            {
                "id": "retained-recreate",
                "taskId": retained["id"],
                "type": "upsert",
                "title": retained["title"],
                "hlcWallMs": 10,
                "hlcCounter": 2,
            },
            {
                "id": "deleted-upsert",
                "taskId": deleted["id"],
                "type": "upsert",
                "title": deleted["title"],
                "hlcWallMs": 9,
                "hlcCounter": 0,
            },
            {
                "id": "deleted-delete",
                "taskId": deleted["id"],
                "type": "delete",
                "hlcWallMs": 11,
                "hlcCounter": 0,
            },
        ]

        for arrival_order in permutations(operations):
            ordered = list(arrival_order)
            projection = rebuild_tasks([], ordered)
            self.assertEqual(projection, [retained])
            self.assertEqual(rebuild_tasks([], ordered + ordered), projection)
            self.assertEqual(rebuild_tasks(projection, ordered), projection)

    def test_malformed_task_upserts_are_no_ops(self) -> None:
        oversized = "x" * 513
        malformed = [
            {
                "id": "non-string",
                "taskId": task_id_for_title("123"),
                "type": "upsert",
                "title": 123,
            },
            {
                "id": "empty",
                "taskId": task_id_for_title(""),
                "type": "upsert",
                "title": "\x00",
            },
            {
                "id": "oversized",
                "taskId": task_id_for_title(oversized),
                "type": "upsert",
                "title": oversized,
            },
            {
                "id": "missing-task-id",
                "type": "upsert",
                "title": "Missing identity",
            },
            {
                "id": "mismatched-task-id",
                "taskId": task_id_for_title("Different title"),
                "type": "upsert",
                "title": "Valid title",
            },
        ]

        self.assertEqual(rebuild_tasks([], malformed), [])

    def test_auto_start_lww_is_permutation_invariant_and_idempotent(self) -> None:
        operations = [
            {
                "id": "operation-old",
                "deviceId": "device-z",
                "enabled": True,
                "hlcWallMs": 9,
                "hlcCounter": 99,
            },
            {
                "id": "operation-counter",
                "deviceId": "device-a",
                "enabled": False,
                "hlcWallMs": 10,
                "hlcCounter": 1,
            },
            {
                "id": "operation-device",
                "deviceId": "device-z",
                "enabled": True,
                "hlcWallMs": 10,
                "hlcCounter": 1,
            },
            {
                "id": "operation-z",
                "deviceId": "device-z",
                "enabled": False,
                "hlcWallMs": 10,
                "hlcCounter": 1,
            },
        ]

        self.assertFalse(project_auto_start_breaks(False, []))
        self.assertTrue(project_auto_start_breaks(True, []))
        for arrival_order in permutations(operations):
            ordered = list(arrival_order)
            for base in (False, True):
                projection = project_auto_start_breaks(base, ordered)
                self.assertFalse(projection)
                self.assertEqual(
                    project_auto_start_breaks(base, ordered + ordered), projection
                )

    def test_timer_task_reaches_history_and_daily_summary(self) -> None:
        task = task_from_title("Release")
        timer, history = reduce_command(None, [], command("start", task_id=task["id"]))
        timer, history = reduce_command(
            timer, history, command("finish", 2, task_id="ignored-task-id")
        )
        self.assertEqual(timer["taskId"], task["id"])
        self.assertEqual(history[0]["taskId"], task["id"])
        summaries = task_summaries_today(
            [task], history, datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(summaries[task["id"]], {"finished": 1, "timeMs": 1_500_000})


if __name__ == "__main__":
    unittest.main()
