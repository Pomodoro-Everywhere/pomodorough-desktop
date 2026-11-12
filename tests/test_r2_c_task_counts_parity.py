"""UX round 2 batch C follow-up F2: task totals match Core task_counts.

core.task_summaries_today and counted_run_ms still compute the Tasks screen
totals in the client, including the fallback for rows stored before
elapsedMs existed. Until Desktop reads them from the Core read model (a Core
0.56.0 adoption item), this fixture runs the same history through Core
workspace.readModel.v1 and through the client code and asserts equal totals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pomodorough.core import task_from_title, task_summaries_today
from pomodorough.shared_core import SharedCore

PLANNED_MS = 1_500_000
TASK_A = task_from_title("Architecture")
TASK_B = task_from_title("Billing")
# A task the workspace no longer has; neither side counts its rows.
GONE = task_from_title("Removed")
NOW = datetime(2026, 8, 21, 12, 0).astimezone()
DAY_START = NOW.replace(hour=0)
DAY_END = DAY_START + timedelta(days=1)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row(name: str, task: dict[str, Any], status: str, **changes: Any) -> dict[str, Any]:
    at = changes.pop("at", NOW - timedelta(hours=1))
    row: dict[str, Any] = {
        "id": f"history-{name}",
        "timerId": f"timer-{name}",
        "taskId": task["id"],
        "phase": "focus",
        "status": status,
        "plannedDurationMs": PLANNED_MS,
        "completedAt" if status == "completed" else "endedAt": _utc(at),
    }
    row.update(changes)
    return row


# Old rows have no elapsedMs. Cancelled and superseded rows never count.
HISTORY = [
    _row("early", TASK_A, "completed", elapsedMs=420_000),
    _row("old", TASK_A, "completed"),
    _row("full", TASK_B, "completed", elapsedMs=PLANNED_MS),
    _row("zero", TASK_B, "completed", elapsedMs=0),
    _row("half-minute", TASK_B, "completed", elapsedMs=450_000),
    _row("cancelled", TASK_A, "cancelled", elapsedMs=600_000),
    _row("cancelled-old", TASK_B, "cancelled"),
    _row("superseded", TASK_B, "superseded", elapsedMs=300_000),
    _row("superseded-old", TASK_A, "superseded"),
    _row("yesterday", TASK_A, "completed", elapsedMs=60_000, at=DAY_START - timedelta(minutes=1)),
    _row("break", TASK_A, "completed", phase="short_break", plannedDurationMs=300_000),
    _row("gone", GONE, "completed", elapsedMs=120_000),
]


def _core_task_counts(history: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    workspace = {
        "base": {
            "canonicalTimer": None,
            "history": history,
            "tasks": [TASK_A, TASK_B],
            "durationsMs": {"focus": PLANNED_MS, "short_break": 300_000, "long_break": 900_000},
            "autoStartBreaks": False,
            "selectedTaskId": None,
        },
        "local": {"commands": [], "taskOperations": [], "durationOperations": [],
                  "autoStartOperations": [], "selectedTaskOperations": []},
        "canonicalHead": {"wallMs": int(NOW.timestamp() * 1000), "counter": 0},
        "neverSent": {},
        "timerDependencies": [],
    }
    output = SharedCore().dispatch(
        "workspace.readModel.v1",
        {
            "profile": "desktopStorage",
            "source": {"kind": "workspace", "value": workspace},
            "selectedPhase": "focus",
            "observedAt": _utc(NOW),
            "calendarIntervals": [{"start": _utc(DAY_START), "end": _utc(DAY_END)}],
        },
    )
    counts = output["tasks"]["completedFocusTodayByTask"]
    return {
        task_id: {"finished": count["count"], "timeMs": count["elapsedMs"]}
        for task_id, count in counts.items()
    }


def test_the_fixture_covers_every_counting_case():
    statuses = {(row["status"], "elapsedMs" in row) for row in HISTORY}
    assert statuses >= {
        ("completed", True), ("completed", False), ("cancelled", True),
        ("cancelled", False), ("superseded", True), ("superseded", False),
    }


def test_client_task_totals_match_core_task_counts():
    client = task_summaries_today([TASK_A, TASK_B], HISTORY, now=NOW)
    core = _core_task_counts(HISTORY)
    assert client == core
    # The plan of the old row counts; cancelled and superseded rows do not.
    assert core[TASK_A["id"]] == {"finished": 2, "timeMs": 420_000 + PLANNED_MS}
    assert core[TASK_B["id"]] == {"finished": 3, "timeMs": PLANNED_MS + 450_000}


@pytest.mark.parametrize("index", range(len(HISTORY)))
def test_each_row_alone_counts_the_same_on_both_sides(index):
    history = [HISTORY[index]]
    client = task_summaries_today([TASK_A, TASK_B], history, now=NOW)
    assert client == _core_task_counts(history)
