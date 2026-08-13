from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
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
_RFC3339_OFFSET = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,9})?(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


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
        task_id = operation.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            continue
        if operation.get("type") == "delete":
            tasks.pop(task_id, None)
        elif operation.get("type") == "upsert":
            title = operation.get("title")
            if not isinstance(title, str):
                continue
            try:
                task = task_from_title(title)
            except ValueError:
                continue
            if task_id == task["id"]:
                tasks[task_id] = task
    return sorted(
        tasks.values(),
        key=lambda task: (task["title"].encode("utf-8"), task["id"].encode("utf-8")),
    )


def project_auto_start_breaks(
    canonical: bool, pending: list[dict[str, Any]]
) -> bool:
    enabled = canonical
    for operation in sorted(
        pending,
        key=lambda item: (
            int(item.get("hlcWallMs", 0)),
            int(item.get("hlcCounter", 0)),
            str(item.get("deviceId", "")),
            str(item.get("id", "")),
        ),
    ):
        if isinstance(operation.get("enabled"), bool):
            enabled = operation["enabled"]
    return enabled


def project_durations(
    canonical: dict[str, int], pending: list[dict[str, Any]]
) -> dict[str, int]:
    durations = {
        phase: int(canonical.get(phase, definition["default_minutes"] * 60_000))
        for phase, definition in PHASES.items()
    }
    for operation in sorted(
        pending,
        key=lambda item: (
            int(item.get("hlcWallMs", 0)),
            int(item.get("hlcCounter", 0)),
            str(item.get("id", "")),
        ),
    ):
        phase = operation.get("phase")
        duration_ms = operation.get("durationMs")
        if (
            phase in PHASES
            and isinstance(duration_ms, int)
            and not isinstance(duration_ms, bool)
            and 60_000 <= duration_ms <= 10_800_000
            and duration_ms % 60_000 == 0
        ):
            durations[phase] = duration_ms
    return durations


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


def completed_focus_count_for_day(
    history: list[dict[str, Any]], now: datetime | str | None = None
) -> int:
    if isinstance(now, str):
        try:
            reference = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            return 0
    else:
        reference = now or datetime.now().astimezone()
    local_day = reference.astimezone().date()
    count = 0
    for item in history:
        if item.get("phase") != "focus" or item.get("status") != "completed":
            continue
        completed_at = item.get("completedAt") or item.get("endedAt")
        try:
            completed_day = datetime.fromisoformat(
                str(completed_at).replace("Z", "+00:00")
            ).astimezone().date()
        except (TypeError, ValueError):
            continue
        if completed_day == local_day:
            count += 1
    return count


def long_break_progress(completed_focus_count: int) -> int:
    return ((completed_focus_count - 1) % 4) + 1 if completed_focus_count > 0 else 0


def parse_timestamp_ms(value: str | None) -> int | None:
    if not value or _RFC3339_OFFSET.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return int(parsed.timestamp() * 1000)
    except (OverflowError, TypeError, ValueError):
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

    def add_terminal_history(source: dict[str, Any], status: str) -> None:
        if any(item.get("commandId") == command["id"] for item in next_history):
            return
        item = {
            "id": f'{source["id"]}:{command["id"]}',
            "timerId": source["id"],
            "commandId": command["id"],
            "phase": source["phase"],
            "status": status,
            "plannedDurationMs": source["plannedDurationMs"],
            "endedAt": command["occurredAt"],
            "pending": True,
            "taskId": source.get("taskId"),
        }
        if status == "completed":
            item["completedAt"] = command["occurredAt"]
        next_history.insert(0, item)

    if next_timer is not None and next_timer.get("status") == "running":
        occurred_ms = parse_timestamp_ms(command.get("occurredAt"))
        anchor_ms = parse_timestamp_ms(next_timer.get("anchorAt"))
        planned = max(0, int(next_timer.get("plannedDurationMs") or 0))
        stored_elapsed = min(
            planned, max(0, int(next_timer.get("elapsedAtAnchorMs") or 0))
        )
        if (
            occurred_ms is not None
            and anchor_ms is not None
            and stored_elapsed + max(0, occurred_ms - anchor_ms) >= planned
        ):
            completed_ms = anchor_ms + planned - stored_elapsed
            completed_at = (
                datetime.fromtimestamp(completed_ms / 1000, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            next_timer.update(
                status="completed",
                elapsedAtAnchorMs=planned,
                anchorAt=completed_at,
            )
            if not any(
                item.get("timerId") == next_timer["id"] for item in next_history
            ):
                next_history.insert(
                    0,
                    {
                        "id": next_timer["id"],
                        "timerId": next_timer["id"],
                        "phase": next_timer["phase"],
                        "status": "completed",
                        "plannedDurationMs": planned,
                        "completedAt": completed_at,
                        "endedAt": completed_at,
                        "taskId": next_timer.get("taskId"),
                    },
                )

    if command_type == "start":
        timer_id = command["timerId"]
        if (
            (next_timer is not None and next_timer.get("id") == timer_id)
            or any(item.get("timerId") == timer_id for item in next_history)
        ):
            return next_timer, next_history
        if next_timer is not None and next_timer.get("status") in ACTIVE_STATUSES:
            add_terminal_history(next_timer, "superseded")
        return (
            {
                "id": timer_id,
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

    if command_type == "resume" and (
        not next_timer or command.get("timerId") != next_timer.get("id")
    ):
        superseded = next(
            (
                item
                for item in next_history
                if item.get("timerId") == command.get("timerId")
                and item.get("status") == "superseded"
            ),
            None,
        )
        if superseded is not None:
            if next_timer is not None and next_timer.get("status") in ACTIVE_STATUSES:
                add_terminal_history(next_timer, "superseded")
            next_history = [item for item in next_history if item is not superseded]
            planned = int(superseded["plannedDurationMs"])
            return (
                {
                    "id": superseded["timerId"],
                    "phase": superseded["phase"],
                    "status": "running",
                    "plannedDurationMs": planned,
                    "elapsedAtAnchorMs": min(
                        planned,
                        max(0, int(command.get("observedElapsedMs") or 0)),
                    ),
                    "anchorAt": command["occurredAt"],
                    "lastIntent": intent,
                    "taskId": superseded.get("taskId"),
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
    elif command_type == "resume" and status in {"paused", "superseded"}:
        if status == "superseded":
            next_history = [
                item
                for item in next_history
                if not (
                    item.get("timerId") == next_timer["id"]
                    and item.get("status") == "superseded"
                )
            ]
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
        add_terminal_history(next_timer, "completed")
    elif command_type == "finish" and status == "completed":
        completion = next(
            (
                item
                for item in next_history
                if item.get("timerId") == next_timer["id"]
                and item.get("status") == "completed"
                and not item.get("commandId")
            ),
            None,
        )
        if completion is not None:
            next_timer["lastIntent"] = intent
            completion["commandId"] = command["id"]
            completion["pending"] = True
    elif command_type == "cancel" and status in ACTIVE_STATUSES:
        next_timer.update(
            status="cancelled",
            elapsedAtAnchorMs=observed,
            anchorAt=command["occurredAt"],
            lastIntent=intent,
        )
        add_terminal_history(next_timer, "cancelled")
    elif command_type == "clear" and status in {"completed", "cancelled"}:
        return None, next_history

    return next_timer, next_history


def rebuild_optimistic(
    base_timer: dict[str, Any] | None,
    base_history: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    timer = deepcopy(base_timer)
    history = deepcopy(base_history)
    for command in sorted(
        pending, key=lambda item: (int(item["deviceSequence"]), str(item["id"]))
    ):
        timer, history = reduce_command(timer, history, command)
    return timer, history


def format_remaining(duration_ms: int) -> str:
    total_seconds = max(0, ceil(duration_ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def next_break_phase(
    history: list[dict[str, Any]], now: datetime | str | None = None
) -> str:
    completed_focus = completed_focus_count_for_day(history, now)
    return "long_break" if completed_focus > 0 and completed_focus % 4 == 0 else "short_break"
