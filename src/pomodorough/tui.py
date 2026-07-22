from __future__ import annotations

import argparse
import curses
import sys
from pathlib import Path
from typing import Any, Sequence

from .storage import Store
from .terminal import InvalidAction, LocalTimer

STATUS_LABELS = {
    "idle": "READY AT PLATFORM",
    "running": "IN TRANSIT",
    "paused": "HELD AT SIGNAL",
    "completed": "ARRIVED",
    "cancelled": "SERVICE CANCELLED",
    "superseded": "ROUTE CHANGED",
}


def build_lines(
    state: dict[str, Any],
    history: list[dict[str, Any]],
    width: int,
    message: str = "",
) -> list[str]:
    bar_width = max(10, min(48, width - 12))
    filled = round(state["progress"] * bar_width)
    progress = "#" * filled + "-" * (bar_width - filled)
    lines = [
        "POMODOROUGH",
        "TIME, IN TRANSIT",
        "",
        state["phaseLabel"].upper(),
        state["remaining"],
        STATUS_LABELS.get(state["status"], state["status"].upper()),
        f"[{progress}]",
        "",
        "space start/pause/resume  f finish  x cancel  c clear",
        "1 focus  2 short break  3 long break  +/- duration  q quit",
    ]
    if state["taskTitle"]:
        lines.insert(6, f"TASK: {state['taskTitle']}")
    if state["pendingCommands"]:
        lines.append(f"{state['pendingCommands']} command(s) pending sync")
    if state["pendingDurationOperations"]:
        lines.append(
            f"{state['pendingDurationOperations']} duration preference(s) pending sync"
        )
    if state["historyResolutionPending"]:
        lines.append("Account history resolution pending; timer changes are blocked.")
    if message:
        lines.extend(("", message))
    lines.extend(("", "RECENT ARRIVALS"))
    if not history:
        lines.append("No completed timers.")
    for item in history[:5]:
        phase = str(item.get("phase", "timer")).replace("_", " ").title()
        if item.get("taskTitle"):
            phase = f"{phase}: {item['taskTitle']}"
        minutes = int(item.get("plannedDurationMs", 0)) // 60_000
        pending = " *" if item.get("pending") else ""
        lines.append(f"{phase:<12} {minutes:>3} min{pending}")
    return lines


def handle_key(timer: LocalTimer, key: int) -> bool:
    if key in (ord("q"), ord("Q")):
        return False
    if key == ord(" "):
        timer.primary()
    elif key in (ord("f"), ord("F")):
        timer.issue("finish")
    elif key in (ord("x"), ord("X")):
        timer.issue("cancel")
    elif key in (ord("c"), ord("C")):
        timer.issue("clear")
    elif key == ord("1"):
        timer.select_phase("focus")
    elif key == ord("2"):
        timer.select_phase("short_break")
    elif key == ord("3"):
        timer.select_phase("long_break")
    elif key in (ord("+"), ord("=")):
        timer.adjust_duration(1)
    elif key in (ord("-"), ord("_")):
        timer.adjust_duration(-1)
    return True


def _draw(screen: Any, timer: LocalTimer, message: str) -> None:
    height, width = screen.getmaxyx()
    state = timer.state(auto_finish=True)
    lines = build_lines(state, timer.completed_history(5), width, message)
    screen.erase()
    if height < 12 or width < 40:
        screen.addnstr(0, 0, "Terminal too small (minimum 40x12).", max(1, width - 1))
    else:
        for row, line in enumerate(lines[: height - 1]):
            attribute = curses.A_BOLD if row in (0, 3, 4) else curses.A_NORMAL
            screen.addnstr(row, 0, line, max(1, width - 1), attribute)
    screen.refresh()


def _run(screen: Any, timer: LocalTimer) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(250)
    message = ""
    while True:
        try:
            _draw(screen, timer, message)
            key = screen.getch()
            if key == -1:
                continue
            message = ""
            if not handle_key(timer, key):
                return
        except (InvalidAction, OSError) as error:
            message = str(error)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pomodorough-tui",
        description="Run Pomodorough in a terminal.",
    )
    parser.add_argument("--data", type=Path, help="use a custom SQLite database")
    args = parser.parse_args(argv)
    store = Store(args.data.expanduser() if args.data else None)
    try:
        curses.wrapper(_run, LocalTimer(store))
        return 0
    except KeyboardInterrupt:
        return 130
    except curses.error as error:
        print(f"pomodorough-tui: terminal unavailable: {error}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
