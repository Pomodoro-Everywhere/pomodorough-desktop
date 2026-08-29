from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .core import parse_timestamp_ms, task_from_title
from .shared_core import (
    SharedCoreError,
    TimerCompletionPlanV1,
    apply_projection_v2,
    plan_timer_completion_v1,
)
from .storage_canonical_reconciliation import generated_break_day_bounds
from .storage_model import _default_shared_core, utc_timestamp
from .storage_workspace import WorkspacePersistence


@dataclass(frozen=True)
class ProjectionDependencies:
    connection: sqlite3.Connection
    load_state: Callable[[], dict[str, Any]]
    read_meta: Callable[[str, Any], Any]
    normalize_settings: Callable[[Any], dict[str, Any]]
    logical_clock: Callable[..., tuple[int, int]]
    project_operation: Callable[..., Any]
    display_minutes: Callable[[int], int]
    valid_canonical_timer: Callable[[Any], bool]
    valid_history_item: Callable[[Any], bool]
    shared_core: Callable[[], Any]
    workspace: WorkspacePersistence


class GeneratedBreakPlanner:
    def __init__(self, shared_core: Callable[[], Any]) -> None:
        self._shared_core = shared_core

    def plan(
        self,
        before: Any,
        projection: dict[str, Any],
        settings: dict[str, Any],
        device_id: str,
    ) -> TimerCompletionPlanV1:
        timer = projection.get("canonicalTimer")
        day_start, day_end = self._day_bounds(timer)
        input_value = {
            "kind": "expiry",
            "beforeTimer": before if isinstance(before, dict) else None,
            "projectedTimer": timer if isinstance(timer, dict) else None,
            "history": projection["history"],
            "selectedPhase": settings["selectedPhase"],
            "autoStartBreaks": projection["autoStartBreaks"],
            "localDeviceId": device_id,
            "ownership": self._ownership(timer),
            "dayStart": day_start,
            "dayEnd": day_end,
        }
        core = self._shared_core() or _default_shared_core()
        try:
            return plan_timer_completion_v1(core, input_value)
        except SharedCoreError as error:
            raise ValueError(str(error)) from error

    @staticmethod
    def _day_bounds(timer: Any) -> tuple[str, str]:
        if not isinstance(timer, dict) or not isinstance(timer.get("anchorAt"), str):
            return "", ""
        return generated_break_day_bounds(parse_timestamp_ms(timer["anchorAt"]))

    @staticmethod
    def _ownership(timer: Any) -> dict[str, str] | None:
        if not isinstance(timer, dict):
            return None
        timer_id = timer.get("id")
        owner = timer.get("startedByDeviceId")
        if not isinstance(timer_id, str) or not isinstance(owner, str):
            return None
        return {"timerId": timer_id, "ownerDeviceId": owner}


class ReplicatedStateProjection:
    def __init__(self, dependencies: ProjectionDependencies) -> None:
        self._dependencies = dependencies

    def projected_local_genesis(self) -> dict[str, Any]:
        state = self._dependencies.load_state()
        settings = self._dependencies.normalize_settings(state["settings"])
        projection = self._dependencies.project_operation(
            settings,
            now=utc_timestamp(int(time.time() * 1000)),
        )
        wall, counter = self._max_local_clock(state)
        return {
            "canonicalTimer": self._clean_local_timer(projection.canonical_timer),
            "history": self.clean_history(projection.history),
            "tasks": projection.tasks,
            "durationsMs": projection.durations_ms,
            "autoStartBreaks": projection.auto_start_breaks,
            "selectedTaskId": projection.selected_task_id,
            "hlcWallMs": wall,
            "hlcCounter": counter,
        }

    def _max_local_clock(self, state: dict[str, Any]) -> tuple[int, int]:
        clocks = [
            (int(operation.get("hlcWallMs", 0)), int(operation.get("hlcCounter", 0)))
            for operations in (
                state["pending"],
                state["pendingTasks"],
                state["pendingDurations"],
                state["pendingAutoStarts"],
                state["pendingSelectedTasks"],
            )
            for operation in operations
        ]
        clocks.append(
            self._dependencies.logical_clock(
                self._dependencies.read_meta(
                    "hlc",
                    {"wallMs": 0, "counter": 0},
                ),
                allow_legacy_zero=True,
            )
        )
        return max(clocks, default=(0, 0))

    @staticmethod
    def clean_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean_history = []
        for item in history:
            cleaned = deepcopy(item)
            cleaned.pop("pending", None)
            for key in ("commandId", "taskId", "completedAt", "endedAt"):
                if cleaned.get(key) is None:
                    cleaned.pop(key, None)
            clean_history.append(cleaned)
        clean_history.sort(key=ReplicatedStateProjection._history_order)
        return clean_history

    @staticmethod
    def _clean_local_timer(
        timer: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        clean_timer = deepcopy(timer)
        if clean_timer is not None:
            for key in ("taskId", "startedByDeviceId", "lastIntent"):
                if clean_timer.get(key) is None:
                    clean_timer.pop(key, None)
            intent = clean_timer.get("lastIntent")
            if isinstance(intent, dict):
                intent.pop("deviceId", None)
        return clean_timer

    @staticmethod
    def _history_order(item: dict[str, Any]) -> tuple[int, bytes]:
        timestamp = item.get("endedAt") or item.get("completedAt")
        milliseconds = (
            parse_timestamp_ms(timestamp) if isinstance(timestamp, str) else None
        )
        return (-(milliseconds or 0), str(item.get("timerId", "")).encode("utf-8"))

    def empty_workspace(self, genesis: dict[str, Any]) -> dict[str, Any]:
        workspace = self._dependencies.workspace.capture()
        settings = self._dependencies.normalize_settings(
            workspace["metadata"]["settings"]
        )
        settings["durationsMs"] = deepcopy(genesis["durationsMs"])
        settings["durations"] = {
            phase: self._dependencies.display_minutes(duration)
            for phase, duration in genesis["durationsMs"].items()
        }
        settings["autoStartBreaks"] = genesis["autoStartBreaks"]
        settings["selectedTaskId"] = genesis["selectedTaskId"]
        workspace["metadata"].update(
            settings=settings,
            snapshot={
                "revision": 0,
                "canonicalTimer": deepcopy(genesis["canonicalTimer"]),
                "history": deepcopy(genesis["history"]),
                "tasks": sorted(
                    deepcopy(genesis["tasks"]),
                    key=lambda item: (item["title"].encode(), item["id"].encode()),
                ),
                "knownTasks": sorted(
                    deepcopy(genesis["tasks"]),
                    key=lambda item: (item["title"].encode(), item["id"].encode()),
                ),
                "autoStartBreaks": genesis["autoStartBreaks"],
                "selectedTaskId": settings["selectedTaskId"],
                "user": None,
            },
            hlc={"wallMs": genesis["hlcWallMs"], "counter": genesis["hlcCounter"]},
            serverClockSample=None,
            commandPhysicalTimes={},
            pendingSync=None,
            pendingResolution=None,
        )
        for table in workspace["tables"]:
            workspace["tables"][table] = []
        return workspace

    def empty_joined_workspace(
        self,
        return_workspace: dict[str, Any],
    ) -> dict[str, Any]:
        genesis = {
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": self._dependencies.normalize_settings(
                return_workspace["metadata"]["settings"]
            )["durationsMs"],
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "hlcWallMs": 0,
            "hlcCounter": 0,
        }
        return self.empty_workspace(genesis)

    def workspace_with_projection(
        self,
        workspace: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        settings = self._dependencies.normalize_settings(
            workspace["metadata"]["settings"]
        )
        settings["durationsMs"] = deepcopy(projection["durationsMs"])
        settings["durations"] = {
            phase: self._dependencies.display_minutes(duration)
            for phase, duration in projection["durationsMs"].items()
        }
        settings["autoStartBreaks"] = projection["autoStartBreaks"]
        settings["selectedTaskId"] = projection["selectedTaskId"]
        previous = workspace["metadata"].get("snapshot", {})
        known = {
            item["id"]: item
            for item in previous.get("knownTasks", [])
            if isinstance(item, dict) and item.get("id") and item.get("title")
        }
        known.update(
            {
                item["id"]: item
                for item in projection.get("knownTasks", projection["tasks"])
            }
        )
        workspace["metadata"].update(
            settings=settings,
            snapshot=self._projection_snapshot(projection, settings, known),
            hlc={
                "wallMs": projection["hlcWallMs"],
                "counter": projection["hlcCounter"],
            },
            serverClockSample=None,
            commandPhysicalTimes={},
            pendingSync=None,
            pendingResolution=None,
        )
        for table in (
            "pending_commands",
            "pending_task_operations",
            "pending_duration_operations",
            "pending_auto_start_operations",
            "pending_selected_task_operations",
            "pending_auto_break_starts",
            "pending_phase_advances",
        ):
            workspace["tables"][table] = []
        return workspace

    @staticmethod
    def _projection_snapshot(
        projection: dict[str, Any],
        settings: dict[str, Any],
        known: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "revision": 0,
            "canonicalTimer": projection["canonicalTimer"],
            "history": projection["history"],
            "tasks": projection["tasks"],
            "knownTasks": sorted(
                known.values(),
                key=lambda item: (item["title"].encode(), item["id"].encode()),
            ),
            "autoStartBreaks": projection["autoStartBreaks"],
            "selectedTaskId": settings["selectedTaskId"],
            "user": None,
        }

    def project_room(
        self,
        room_id: str,
        *,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        genesis, pending, clocks, known_tasks = self._projection_inputs(room_id)
        projection = self._apply_projection(genesis, pending, now_ms)
        return self._validated_projection(projection, clocks, known_tasks)

    def _validated_room_records(
        self,
        room_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from .iroh_protocol import validate_record

        rows = self._dependencies.connection.execute(
            "SELECT record FROM iroh_records WHERE room_id = ?",
            (room_id,),
        ).fetchall()
        records = [json.loads(row["record"]) for row in rows]
        for record in records:
            validate_record(record)
        genesis_records = [
            record for record in records if record["domain"] == "genesis"
        ]
        if len(genesis_records) != 1:
            raise ValueError("Iroh room genesis is missing or conflicting.")
        return records, deepcopy(genesis_records[0]["operation"])

    def _projection_inputs(
        self,
        room_id: str,
    ) -> tuple[
        dict[str, Any],
        dict[str, list[dict[str, Any]]],
        list[tuple[int, int]],
        dict[str, Any],
    ]:
        from .iroh_protocol import operation_order

        records, genesis = self._validated_room_records(room_id)
        clocks = [(genesis["hlcWallMs"], genesis["hlcCounter"])]
        known_tasks = {task["id"]: task for task in genesis["tasks"]}
        pending: dict[str, list[dict[str, Any]]] = {
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        }
        domain_keys = {
            "timer": "commands",
            "task": "taskOperations",
            "duration": "durationOperations",
            "autoStart": "autoStartOperations",
            "selectedTask": "selectedTaskOperations",
        }
        for record in sorted(
            (record for record in records if record["domain"] != "genesis"),
            key=operation_order,
        ):
            operation = record["operation"]
            clocks.append((operation["hlcWallMs"], operation["hlcCounter"]))
            projected_operation = {**operation, "deviceId": record["deviceId"]}
            pending[domain_keys[record["domain"]]].append(projected_operation)
            if record["domain"] == "task" and operation.get("type") == "upsert":
                try:
                    known = task_from_title(operation.get("title", ""))
                except ValueError:
                    known = None
                if known is not None and known["id"] == operation.get("taskId"):
                    known_tasks[known["id"]] = known
        return genesis, pending, clocks, known_tasks

    def _apply_projection(
        self,
        genesis: dict[str, Any],
        pending: dict[str, Any],
        now_ms: int | None,
    ) -> Any:
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        base_timer = genesis["canonicalTimer"]
        base_history = genesis["history"]
        if isinstance(base_timer, dict) and any(
            item.get("timerId") == base_timer.get("id") for item in base_history
        ):
            base_timer = None
        projection_input = {
            "base": {
                "canonicalTimer": base_timer,
                "history": base_history,
                "tasks": genesis["tasks"],
                "durationsMs": genesis["durationsMs"],
                "autoStartBreaks": genesis["autoStartBreaks"],
                "selectedTaskId": genesis["selectedTaskId"],
            },
            "pending": pending,
            "now": utc_timestamp(now_ms),
        }
        core = self._dependencies.shared_core()
        if core is None:
            core = _default_shared_core()
        try:
            return apply_projection_v2(core, projection_input)
        except SharedCoreError as error:
            raise ValueError(str(error)) from error

    def _validated_projection(
        self,
        projection: Any,
        clocks: list[tuple[int, int]],
        known_tasks: dict[str, Any],
    ) -> dict[str, Any]:
        timer = projection.canonical_timer
        clean_history = self.clean_history(projection.history)
        if timer is not None and not self._dependencies.valid_canonical_timer(timer):
            raise ValueError("Iroh room projected an invalid canonical timer.")
        if any(
            not self._dependencies.valid_history_item(item) for item in clean_history
        ):
            raise ValueError("Iroh room projected invalid timer history.")
        wall, counter = max(clocks)
        return {
            "canonicalTimer": timer,
            "history": clean_history,
            "tasks": projection.tasks,
            "knownTasks": sorted(
                known_tasks.values(),
                key=lambda item: (item["title"].encode(), item["id"].encode()),
            ),
            "durationsMs": projection.durations_ms,
            "autoStartBreaks": projection.auto_start_breaks,
            "selectedTaskId": projection.selected_task_id,
            "hlcWallMs": wall,
            "hlcCounter": counter,
        }
