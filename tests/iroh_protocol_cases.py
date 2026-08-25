from __future__ import annotations

from copy import deepcopy
from typing import Any

from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import room_id_for_secret
from pomodorough.storage import utc_timestamp
from pomodorough.uuid7 import uuid7_from_parts

SECRET = bytes(range(32))
ROOM_ID = room_id_for_secret(SECRET)
REQUEST_ID = uuid7_from_parts(1_786_000_000_000, 1)
DEVICE_ID = "device-matrix-0001"
OPERATION_ID = "operation-matrix-0001"
TIMESTAMP_MS = 1_786_000_000_000
TIMESTAMP = utc_timestamp(TIMESTAMP_MS)


def envelope(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "roomId": ROOM_ID,
        "requestId": REQUEST_ID,
        "kind": kind,
        **fields,
    }


def auto_start_record() -> dict[str, Any]:
    return {
        "domain": "autoStart",
        "deviceId": DEVICE_ID,
        "operation": {
            "id": OPERATION_ID,
            "enabled": True,
            "occurredAt": TIMESTAMP,
            "hlcWallMs": TIMESTAMP_MS,
            "hlcCounter": 0,
        },
    }


def selected_task_record() -> dict[str, Any]:
    record = auto_start_record()
    record["domain"] = "selectedTask"
    record["operation"] = {
        "id": "selected-operation-0001",
        "taskId": None,
        "occurredAt": TIMESTAMP,
        "hlcWallMs": TIMESTAMP_MS,
        "hlcCounter": 1,
    }
    return record


def timer_record() -> dict[str, Any]:
    record = auto_start_record()
    record["domain"] = "timer"
    record["operation"] = {
        "id": "timer-operation-0001",
        "deviceSequence": 1,
        "timerId": "timer-identity-0001",
        "type": "start",
        "phase": "focus",
        "plannedDurationMs": 1_500_000,
        "occurredAt": TIMESTAMP,
        "hlcWallMs": TIMESTAMP_MS,
        "hlcCounter": 2,
        "observedElapsedMs": 0,
        "taskId": "task-identity-0001",
    }
    return record


def task_record() -> dict[str, Any]:
    record = auto_start_record()
    record["domain"] = "task"
    task = task_from_title("Matrix task")
    record["operation"] = {
        "id": "task-operation-0001",
        "taskId": task["id"],
        "type": "upsert",
        "title": task["title"],
        "occurredAt": TIMESTAMP,
        "hlcWallMs": TIMESTAMP_MS,
        "hlcCounter": 3,
    }
    return record


def duration_record() -> dict[str, Any]:
    record = auto_start_record()
    record["domain"] = "duration"
    record["operation"] = {
        "id": "duration-operation-0001",
        "phase": "short_break",
        "durationMs": 300_000,
        "occurredAt": TIMESTAMP,
        "hlcWallMs": TIMESTAMP_MS,
        "hlcCounter": 4,
    }
    return record


def genesis_record() -> dict[str, Any]:
    return {
        "domain": "genesis",
        "deviceId": DEVICE_ID,
        "operation": {
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 1_500_000,
                "short_break": 300_000,
                "long_break": 900_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "hlcWallMs": 0,
            "hlcCounter": 0,
        },
    }


def changed(value: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(value)
