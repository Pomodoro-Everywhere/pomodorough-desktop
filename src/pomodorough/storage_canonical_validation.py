from __future__ import annotations

from typing import Any, Protocol

from .core import PHASES, parse_timestamp_ms, task_from_title
from .storage_model import (
    CANONICAL_DURATION_MAX_MS,
    MAX_CLOCK_SKEW_MS,
    MAX_SAFE_INTEGER,
)


class CanonicalValidationDependencies(Protocol):
    def _canonical_durations(self, durations_ms: Any) -> dict[str, int]: ...

    def _duration_ms(self, value: Any, *, maximum: int) -> int: ...

    def _logical_clock(self, value: Any) -> tuple[int, int]: ...

    def _physical_time_ms(self, value: Any) -> int: ...


class CanonicalValidationHooks(Protocol):
    def _require_sync_response_fields(self, response: Any) -> None: ...

    def _validated_sync_scalars(self, response: dict[str, Any]) -> tuple[int, bool]: ...

    def _validated_sync_tasks(
        self, tasks: Any
    ) -> tuple[list[dict[str, Any]], list[str]]: ...

    def _validated_sync_acknowledgements(
        self, response: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]: ...

    def _valid_canonical_timer(self, timer: Any) -> bool: ...

    def _valid_history_item(self, item: Any) -> bool: ...

    def _validated_sync_history(self, history: Any) -> list[dict[str, Any]]: ...

    def _validated_sync_server_clock(
        self, response: dict[str, Any]
    ) -> tuple[str, int, int, int]: ...


class DurationMs(Protocol):
    def __call__(self, value: Any, *, maximum: int) -> int: ...


def valid_canonical_timer(
    timer: Any,
    duration_ms: DurationMs,
) -> bool:
    if not isinstance(timer, dict):
        return False
    required = {
        "id",
        "phase",
        "status",
        "plannedDurationMs",
        "elapsedAtAnchorMs",
        "anchorAt",
    }
    if required - set(timer):
        return False
    try:
        planned_ms = duration_ms(
            timer["plannedDurationMs"], maximum=CANONICAL_DURATION_MAX_MS
        )
    except ValueError:
        return False
    elapsed = timer["elapsedAtAnchorMs"]
    if (
        not isinstance(timer["id"], str)
        or not timer["id"]
        or timer["phase"] not in PHASES
        or timer["status"]
        not in {"running", "paused", "completed", "cancelled", "superseded"}
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, int)
        or not 0 <= elapsed <= planned_ms
        or not isinstance(timer["anchorAt"], str)
        or parse_timestamp_ms(timer["anchorAt"]) is None
        or not isinstance(timer.get("taskId"), (str, type(None)))
        or isinstance(timer.get("taskId"), str)
        and not timer["taskId"]
    ):
        return False
    intent = timer.get("lastIntent")
    return intent is None or (
        isinstance(intent, dict)
        and intent.get("type")
        in {"start", "pause", "resume", "finish", "cancel", "clear"}
        and isinstance(intent.get("commandId"), str)
        and bool(intent["commandId"])
        and isinstance(intent.get("occurredAt"), str)
        and parse_timestamp_ms(intent["occurredAt"]) is not None
    )


def valid_history_item(
    item: Any,
    duration_ms: DurationMs,
) -> bool:
    if not isinstance(item, dict):
        return False
    required = {"id", "timerId", "phase", "status", "plannedDurationMs"}
    if required - set(item):
        return False
    try:
        duration_ms(item["plannedDurationMs"], maximum=CANONICAL_DURATION_MAX_MS)
    except ValueError:
        return False
    for timestamp_key in ("completedAt", "endedAt"):
        timestamp = item.get(timestamp_key)
        if timestamp is not None and (
            not isinstance(timestamp, str) or parse_timestamp_ms(timestamp) is None
        ):
            return False
    return (
        isinstance(item["id"], str)
        and bool(item["id"])
        and isinstance(item["timerId"], str)
        and bool(item["timerId"])
        and item["phase"] in PHASES
        and item["status"] in {"completed", "cancelled", "superseded"}
        and isinstance(item.get("commandId"), (str, type(None)))
        and (item.get("commandId") is None or bool(item["commandId"]))
        and isinstance(item.get("taskId"), (str, type(None)))
        and (item.get("taskId") is None or bool(item["taskId"]))
        and isinstance(item.get("pending", False), bool)
    )


def require_sync_response_fields(response: Any) -> None:
    required = {
        "acknowledgements",
        "taskAcknowledgements",
        "durationAcknowledgements",
        "autoStartAcknowledgements",
        "selectedTaskAcknowledgements",
        "revision",
        "canonicalTimer",
        "history",
        "tasks",
        "durationsMs",
        "autoStartBreaks",
        "selectedTaskId",
        "serverTime",
        "serverHlcWallMs",
        "serverHlcCounter",
    }
    if not isinstance(response, dict):
        raise ValueError("Server returned an invalid sync response.")  # noqa: TRY004
    missing = required - set(response)
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"Server response omitted canonical fields: {fields}.")


def validated_sync_scalars(response: dict[str, Any]) -> tuple[int, bool]:
    revision = response["revision"]
    preference = response["autoStartBreaks"]
    if not isinstance(preference, bool):
        raise ValueError(  # noqa: TRY004
            "Server returned an invalid auto-start preference."
        )
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= MAX_SAFE_INTEGER
    ):
        raise ValueError("Server returned an invalid revision.")
    return revision, preference


def validated_sync_tasks(
    tasks: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(tasks, list):
        raise ValueError("Server returned invalid tasks.")  # noqa: TRY004
    ids = []
    for task in tasks:
        try:
            title = task.get("title") if isinstance(task, dict) else None
            normalized = task_from_title(title) if isinstance(title, str) else None
        except ValueError:
            normalized = None
        if normalized is None or normalized["id"] != task.get("id"):
            raise ValueError("Server returned invalid tasks.")
        ids.append(task["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Server returned duplicate tasks.")
    return tasks, ids


class CanonicalWireValidator:
    def __init__(
        self,
        dependencies: CanonicalValidationDependencies,
        hooks: CanonicalValidationHooks,
    ) -> None:
        self._dependencies = dependencies
        self._hooks = hooks

    def _validated_sync_response(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        self._hooks._require_sync_response_fields(response)
        acknowledgements = self._hooks._validated_sync_acknowledgements(
            response, request
        )
        canonical_durations = self._dependencies._canonical_durations(response["durationsMs"])
        revision, auto_start_breaks = self._hooks._validated_sync_scalars(response)
        history = self._hooks._validated_sync_history(response["history"])
        tasks, task_ids = self._hooks._validated_sync_tasks(response["tasks"])
        selected_task_id = response["selectedTaskId"]
        if selected_task_id is not None and (
            not isinstance(selected_task_id, str)
            or not selected_task_id
            or selected_task_id not in task_ids
        ):
            raise ValueError("Server returned an invalid selected-task preference.")
        canonical_timer = response["canonicalTimer"]
        if canonical_timer is not None and not self._hooks._valid_canonical_timer(
            canonical_timer
        ):
            raise ValueError("Server returned an invalid canonical timer.")
        server_time, server_time_ms, server_hlc_wall_ms, server_hlc_counter = (
            self._hooks._validated_sync_server_clock(response)
        )
        return {
            **acknowledgements,
            "revision": revision,
            "canonicalTimer": canonical_timer,
            "history": history,
            "tasks": tasks,
            "durationsMs": canonical_durations,
            "autoStartBreaks": auto_start_breaks,
            "selectedTaskId": selected_task_id,
            "serverTime": server_time,
            "serverTimeMs": server_time_ms,
            "serverHlcWallMs": server_hlc_wall_ms,
            "serverHlcCounter": server_hlc_counter,
        }

    def _validated_sync_server_clock(
        self, response: dict[str, Any]
    ) -> tuple[str, int, int, int]:
        wall = response["serverHlcWallMs"]
        counter = response["serverHlcCounter"]
        server_time = response["serverTime"]
        try:
            clock = self._dependencies._logical_clock({"wallMs": wall, "counter": counter})
            time_ms = (
                parse_timestamp_ms(server_time)
                if isinstance(server_time, str)
                else None
            )
            if time_ms is None:
                raise ValueError
            self._dependencies._physical_time_ms(time_ms)
            if clock[0] < time_ms or clock[0] - time_ms > MAX_CLOCK_SKEW_MS:
                raise ValueError
        except ValueError:
            raise ValueError("Server returned an invalid logical clock.")
        return server_time, time_ms, wall, counter

    def _validated_sync_history(self, history: Any) -> list[dict[str, Any]]:
        if not isinstance(history, list) or any(
            not self._hooks._valid_history_item(item) for item in history
        ):
            raise ValueError("Server returned invalid timer history.")
        ids = [item["id"] for item in history]
        if len(ids) != len(set(ids)):
            raise ValueError("Server returned duplicate timer history.")
        return history

    def _valid_canonical_timer(self, timer: Any) -> bool:
        return valid_canonical_timer(timer, self._dependencies._duration_ms)

    def _valid_history_item(self, item: Any) -> bool:
        return valid_history_item(item, self._dependencies._duration_ms)
