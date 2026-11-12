"""How an arrival row words its status and minutes.

Core 0.55.0 records ``elapsedMs``, the time a session really ran, on every
history row it writes. A row that has it shows that time for every status,
and a completed row that ran shorter than its plan reads as finished early.

Rows stored before the field existed record only the planned duration. A
run that did not complete must not read as that much time spent, so such a
row words the number as planned. The Arrivals screen, the command line and
the terminal view share this rule. Nothing here derives elapsed time; it
only reads what Core recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .core import recorded_elapsed_ms, shown_run_minutes

PLANNED_ONLY_STATUSES = frozenset({"cancelled", "superseded"})


@dataclass(frozen=True)
class ArrivalMinutes:
    status_key: str
    minutes: int
    reads_as_planned: bool


def arrival_minutes(item: Mapping[str, Any]) -> ArrivalMinutes:
    status = str(item.get("status", "completed"))
    planned_ms = int(item.get("plannedDurationMs") or 0)
    elapsed = recorded_elapsed_ms(item)
    if elapsed is None:
        return ArrivalMinutes(
            f"arrivals.status.{status}",
            planned_ms // 60_000,
            status in PLANNED_ONLY_STATUSES,
        )
    finished_early = status == "completed" and elapsed < planned_ms
    return ArrivalMinutes(
        "arrivals.status.finished_early"
        if finished_early
        else f"arrivals.status.{status}",
        shown_run_minutes(elapsed),
        False,
    )
