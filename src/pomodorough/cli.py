from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, TextIO

from .storage import Store
from .terminal import InvalidAction, LocalTimer, normalize_phase


def phase_argument(value: str) -> str:
    try:
        return normalize_phase(value)
    except InvalidAction as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pomodorough-cli",
        description="Control Pomodorough from the command line.",
    )
    parser.add_argument("--data", type=Path, help="use a custom SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show current timer")
    status.add_argument("--json", action="store_true", dest="as_json")

    start = commands.add_parser("start", help="start a timer")
    start.add_argument("phase", nargs="?", type=phase_argument)
    start.add_argument("-m", "--minutes", type=int)
    start.add_argument("--json", action="store_true", dest="as_json")

    for action in ("pause", "resume", "finish", "cancel", "clear"):
        command = commands.add_parser(action, help=f"{action} current timer")
        command.add_argument("--json", action="store_true", dest="as_json")

    history = commands.add_parser("history", help="show completed timers")
    history.add_argument("-n", "--limit", type=int, default=10)
    history.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_state(state: dict[str, Any], stream: TextIO) -> None:
    print(
        f"{state['phaseLabel']} | {state['status']} | {state['remaining']}",
        file=stream,
    )
    if state["taskTitle"]:
        print(f"Task: {state['taskTitle']}", file=stream)
    if state["pendingCommands"]:
        print(f"{state['pendingCommands']} command(s) pending sync", file=stream)
    if state["pendingDurationOperations"]:
        print(
            f"{state['pendingDurationOperations']} duration preference(s) pending sync",
            file=stream,
        )


def _history_time(value: str | None) -> str:
    if not value:
        return "unknown time"
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        return value


def run(args: argparse.Namespace, timer: LocalTimer, stream: TextIO) -> None:
    if args.command == "history":
        if args.limit < 1:
            raise InvalidAction("History limit must be at least 1.")
        history = timer.completed_history(args.limit)
        if args.as_json:
            print(json.dumps(history, indent=2), file=stream)
            return
        if not history:
            print("No completed timers.", file=stream)
            return
        for item in history:
            phase = str(item.get("phase", "timer")).replace("_", " ").title()
            if item.get("taskTitle"):
                phase = f"{phase} | {item['taskTitle']}"
            minutes = int(item.get("plannedDurationMs", 0)) // 60_000
            when = _history_time(item.get("completedAt") or item.get("endedAt"))
            pending = " | pending sync" if item.get("pending") else ""
            print(f"{when} | {phase} | {minutes} min{pending}", file=stream)
        return

    timer.state(auto_finish=True)
    if args.command == "start":
        timer.issue("start", phase=args.phase, minutes=args.minutes)
    elif args.command != "status":
        timer.issue(args.command)

    state = timer.state(auto_finish=True)
    if args.as_json:
        print(json.dumps(state, indent=2), file=stream)
    else:
        _print_state(state, stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Store | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    owns_store = store is None
    store = store or Store(args.data.expanduser() if args.data else None)
    try:
        run(args, LocalTimer(store), stdout)
        return 0
    except (InvalidAction, OSError) as error:
        print(f"pomodorough-cli: {error}", file=stderr)
        return 2
    finally:
        if owns_store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
