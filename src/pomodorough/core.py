from __future__ import annotations

import hashlib
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime
from math import ceil
from typing import Any

PHASES = {
    "focus": {"label": "Focus", "default_minutes": 25},
    "short_break": {"label": "Short break", "default_minutes": 5},
    "long_break": {"label": "Long break", "default_minutes": 15},
}
ACTIVE_STATUSES = {"running", "paused"}
TERMINAL_STATUSES = {"completed", "cancelled", "superseded"}
TASK_NAMESPACE = b"pomodorough.task.v1\x00"


def empty_timer(phase: str, duration_ms: int) -> dict[str, Any]:
    return {
        "id": "",
        "phase": phase,
        "status": "idle",
        "plannedDurationMs": duration_ms,
        "elapsedAtAnchorMs": 0,
        "anchorAt": None,
        "lastIntent": None,
        "taskId": None,
    }


def normalize_task_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    start = 0
    end = len(normalized)
    while start < end and not normalized[start].isprintable():
        start += 1
    while end > start and not normalized[end - 1].isprintable():
        end -= 1
    return normalized[start:end]


def task_id_for_title(value: str) -> str:
    title = normalize_task_title(value)
    digest = bytearray(hashlib.sha256(TASK_NAMESPACE + title.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def task_from_title(value: str) -> dict[str, str]:
    title = normalize_task_title(value)
    if not title:
        raise ValueError("Enter a task name with at least one printable character.")
    if len(title.encode("utf-8")) > 512:
        raise ValueError("Task names must be 512 bytes or fewer.")
    return {"id": task_id_for_title(title), "title": title}


def rebuild_tasks(
    base_tasks: list[dict[str, Any]], pending: list[dict[str, Any]]
) -> list[dict[str, str]]:
    tasks = {
        str(task["id"]): {"id": str(task["id"]), "title": str(task["title"])}
        for task in base_tasks
        if task.get("id") and task.get("title")
    }
    ordered = sorted(
        pending,
        key=lambda item: (
            int(item.get("hlcWallMs", 0)),
            int(item.get("hlcCounter", 0)),
            str(item.get("id", "")),
        ),
    )
    for operation in ordered:
        task_id = str(operation.get("taskId", ""))
        if operation.get("type") == "delete":
            tasks.pop(task_id, None)
        elif operation.get("type") == "upsert" and operation.get("title"):
            title = normalize_task_title(str(operation["title"]))
            if task_id == task_id_for_title(title):
                tasks[task_id] = {"id": task_id, "title": title}
    return sorted(tasks.values(), key=lambda task: (task["title"].casefold(), task["id"]))


def task_summaries_today(
    tasks: list[dict[str, Any]], history: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, dict[str, int]]:
    local_day = (now or datetime.now().astimezone()).astimezone().date()
    summaries = {
        str(task["id"]): {"finished": 0, "timeMs": 0}
        for task in tasks
        if task.get("id")
    }
    for item in history:
        task_id = str(item.get("taskId") or "")
        if (
            task_id not in summaries
            or item.get("phase") != "focus"
            or item.get("status") != "completed"
        ):
            continue
        completed_at = item.get("completedAt") or item.get("endedAt")
        try:
            completed_day = datetime.fromisoformat(
                str(completed_at).replace("Z", "+00:00")
            ).astimezone().date()
        except (TypeError, ValueError):
            continue
        if completed_day == local_day:
            summaries[task_id]["finished"] += 1
            summaries[task_id]["timeMs"] += max(
                0, int(item.get("plannedDurationMs") or 0)
            )
    return summaries


def parse_timestamp_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def elapsed_ms(timer: dict[str, Any] | None, now_ms: int) -> int:
    if not timer:
        return 0
    planned = max(0, int(timer.get("plannedDurationMs") or 0))
    elapsed = min(planned, max(0, int(timer.get("elapsedAtAnchorMs") or 0)))
    if timer.get("status") == "running":
        anchor_ms = parse_timestamp_ms(timer.get("anchorAt"))
        if anchor_ms is not None:
            elapsed += max(0, now_ms - anchor_ms)
    return min(planned, max(0, elapsed))


def reduce_command(
    timer: dict[str, Any] | None,
    history: list[dict[str, Any]],
    command: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    next_timer = deepcopy(timer)
    next_history = deepcopy(history)
    command_type = command["type"]
    intent = {
        "type": command_type,
        "commandId": command["id"],
        "occurredAt": command["occurredAt"],
    }

    if command_type == "start":
        return (
            {
                "id": command["timerId"],
                "phase": command["phase"],
                "status": "running",
                "plannedDurationMs": command["plannedDurationMs"],
                "elapsedAtAnchorMs": 0,
                "anchorAt": command["occurredAt"],
                "lastIntent": intent,
                "taskId": command.get("taskId"),
            },
            next_history,
        )

    if not next_timer or command.get("timerId") != next_timer.get("id"):
        return next_timer, next_history

    planned = int(next_timer.get("plannedDurationMs") or 0)
    observed = min(planned, max(0, int(command.get("observedElapsedMs") or 0)))
    status = next_timer.get("status")

    if command_type == "pause" and status == "running":
        next_timer.update(
            status="paused",
            elapsedAtAnchorMs=observed,
            anchorAt=command["occurredAt"],
            lastIntent=intent,
        )
    elif command_type == "resume" and status == "paused":
        next_timer.update(
            status="running",
            elapsedAtAnchorMs=observed,
            anchorAt=command["occurredAt"],
            lastIntent=intent,
        )
    elif command_type == "finish" and status in ACTIVE_STATUSES:
        next_timer.update(
            status="completed",
            elapsedAtAnchorMs=planned,
            anchorAt=command["occurredAt"],
            lastIntent=intent,
        )
        if not any(item.get("commandId") == command["id"] for item in next_history):
            next_history.insert(
                0,
                {
                    "id": f'{command["timerId"]}:{command["id"]}',
                    "timerId": command["timerId"],
                    "commandId": command["id"],
                    "phase": command["phase"],
                    "status": "completed",
                    "plannedDurationMs": command["plannedDurationMs"],
                    "completedAt": command["occurredAt"],
                    "pending": True,
                    "taskId": next_timer.get("taskId"),
                },
            )
    elif command_type == "cancel" and status in ACTIVE_STATUSES:
        next_timer.update(
            status="cancelled",
            elapsedAtAnchorMs=observed,
            anchorAt=command["occurredAt"],
            lastIntent=intent,
        )
    elif command_type == "clear" and status not in ACTIVE_STATUSES:
        return None, next_history

    return next_timer, next_history


def rebuild_optimistic(
    base_timer: dict[str, Any] | None,
    base_history: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    timer = deepcopy(base_timer)
    history = deepcopy(base_history)
    for command in sorted(pending, key=lambda item: int(item["deviceSequence"])):
        timer, history = reduce_command(timer, history, command)
    return timer, history


def format_remaining(duration_ms: int) -> str:
    total_seconds = max(0, ceil(duration_ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def next_break_phase(history: list[dict[str, Any]]) -> str:
    completed_focus = sum(
        1
        for item in history
        if item.get("phase") == "focus" and item.get("status") == "completed"
    )
    return "long_break" if completed_focus > 0 and completed_focus % 4 == 0 else "short_break"
