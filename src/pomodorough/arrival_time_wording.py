"""When an arrival ended, as the command line and the terminal view word it."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .localization import Strings


def arrival_time(value: str | None, strings: Strings | None = None) -> str:
    """Local date and minute of an ISO timestamp; unparseable text stays as stored."""
    if not value:
        return (strings or Strings()).text("terminal.unknown_time")
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        return value


def arrival_ended_at(item: Mapping[str, Any], strings: Strings) -> str:
    return arrival_time(item.get("completedAt") or item.get("endedAt"), strings)
