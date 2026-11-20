"""Labels of the Timer primary and skip actions.

The Timer screen shows one of these per timer state. The dial fit (DSK-12)
measures every one of them, so the dial does not change its form when a
timer starts or a session ends.
"""

from __future__ import annotations

from .core import PHASES
from .localization import Strings


def primary_label(strings: Strings, status: str, selected_phase: str) -> str:
    if status == "running":
        return strings.text("status.primary.pause")
    if status == "paused":
        return strings.text("status.primary.resume")
    return strings.text(
        "status.primary.start",
        phase=strings.text(f"phase.{selected_phase}").upper(),
    )


def skip_label(strings: Strings, destination: str | None) -> str:
    if destination not in PHASES:
        return strings.text("status.skip_unknown")
    return strings.text(
        "status.skip",
        phase=strings.text(f"phase.{destination}").upper(),
    )


def primary_labels(strings: Strings) -> list[str]:
    """Every label the primary action can show."""
    starts = [primary_label(strings, "idle", phase) for phase in PHASES]
    return [
        *starts,
        primary_label(strings, "running", "focus"),
        primary_label(strings, "paused", "focus"),
    ]


def skip_labels(strings: Strings) -> list[str]:
    """Every label the skip action can show."""
    return [skip_label(strings, destination) for destination in (*PHASES, None)]
