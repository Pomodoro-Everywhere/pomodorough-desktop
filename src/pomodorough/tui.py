from __future__ import annotations

import argparse
import curses
import sys
from pathlib import Path
from typing import Any, Sequence

from .localization import Strings
from .storage import Store
from .terminal import InvalidAction, LocalTimer


def _timer_strings(timer: LocalTimer, strings: Strings | None = None) -> Strings:
    candidate = strings or getattr(timer, "strings", None)
    return candidate if isinstance(candidate, Strings) else Strings()


def build_lines(
    state: dict[str, Any],
    history: list[dict[str, Any]],
    width: int,
    message: str = "",
    *,
    strings: Strings | None = None,
) -> list[str]:
    strings = strings or Strings()
    lines = _timer_lines(state, width, strings)
    lines.extend(_pending_lines(state, strings))
    if message:
        lines.extend(("", message))
    lines.extend(("", strings.text("arrivals.title")))
    lines.extend(_history_lines(history, width, strings))
    return lines


def _timer_lines(
    state: dict[str, Any], width: int, strings: Strings
) -> list[str]:
    display = state.get("display", state)
    bar_width = max(10, min(48, width - 12))
    filled = round(display["progress"] * bar_width)
    progress = "#" * filled + "-" * (bar_width - filled)
    lines = [
        strings.text("brand.name"),
        strings.text("brand.tagline"),
        "",
        strings.text(f"phase.{display.get('phase', 'focus')}").upper(),
        display["remaining"],
        strings.messages.get(
            f"status.rail.{state['status']}", str(state["status"]).upper()
        ),
        f"[{progress}]",
        "",
        strings.text("tui.keys_primary"),
        strings.text("tui.keys_secondary"),
    ]
    if display["taskTitle"]:
        lines.insert(6, strings.text("terminal.task_upper", task=display["taskTitle"]))
    return lines


def _pending_lines(state: dict[str, Any], strings: Strings) -> list[str]:
    lines = []
    for key, count in (
        ("terminal.pending_commands", state["pendingCommands"]),
        ("terminal.pending_durations", state["pendingDurationOperations"]),
        ("terminal.pending_auto_start", state["pendingAutoStartOperations"]),
    ):
        if count:
            lines.append(strings.plural(key, count))
    if state["historyResolutionPending"]:
        lines.append(strings.text("terminal.history_resolution_pending"))
    return lines


def _history_lines(
    history: list[dict[str, Any]], width: int, strings: Strings
) -> list[str]:
    lines = []
    if not history:
        empty_title, empty_detail = strings.text("terminal.history_empty").split("\n", 1)
        lines.extend((empty_title.center(width), empty_detail.center(width)))
    for item in history[:5]:
        phase = strings.text(f"phase.{item.get('phase', 'timer')}").title()
        if item.get("taskTitle"):
            phase = strings.text(
                "tui.phase_task", phase=phase, task=item["taskTitle"]
            )
        status = strings.text(
            f"arrivals.status.{item.get('status', 'completed')}"
        )
        minutes = int(item.get("plannedDurationMs", 0)) // 60_000
        lines.append(
            strings.text(
                "tui.history_row",
                phase=phase,
                status=status,
                minutes=minutes,
                pending=(strings.text("tui.pending_mark") if item.get("pending") else ""),
            )
        )
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
        timer.issue("dismiss")
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


def _draw(
    screen: Any, timer: LocalTimer, message: str, strings: Strings | None = None
) -> None:
    strings = _timer_strings(timer, strings)
    height, width = screen.getmaxyx()
    state = timer.state(auto_finish=True)
    lines = build_lines(
        state, timer.retained_history(5), width, message, strings=strings
    )
    screen.erase()
    if height < 12 or width < 40:
        screen.addnstr(
            0, 0, strings.text("tui.too_small"), max(1, width - 1)
        )
    else:
        for row, line in enumerate(lines[: height - 1]):
            attribute = curses.A_BOLD if row in (0, 3, 4) else curses.A_NORMAL
            screen.addnstr(row, 0, line, max(1, width - 1), attribute)
    screen.refresh()


def _run(screen: Any, timer: LocalTimer, strings: Strings | None = None) -> None:
    strings = _timer_strings(timer, strings)
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


def main(argv: Sequence[str] | None = None, *, locale: str | None = None) -> int:
    strings = Strings(locale)
    parser = argparse.ArgumentParser(
        prog="pomodorough-tui",
        description=strings.text("tui.description"),
    )
    parser.add_argument("--data", type=Path, help=strings.text("terminal.data_help"))
    args = parser.parse_args(argv)
    store = Store(args.data.expanduser() if args.data else None)
    try:
        timer = LocalTimer(store)
        timer.strings = strings
        curses.wrapper(_run, timer)
        return 0
    except KeyboardInterrupt:
        return 130
    except curses.error as error:
        print(strings.text("tui.error", error=error), file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
