from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, TextIO

from .localization import Strings
from .storage import Store
from .terminal import InvalidAction, LocalTimer, normalize_phase


def phase_argument(value: str) -> str:
    try:
        return normalize_phase(value)
    except InvalidAction as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser(strings: Strings | None = None) -> argparse.ArgumentParser:
    strings = strings or Strings()
    parser = argparse.ArgumentParser(
        prog="pomodorough-cli",
        description=strings.text("cli.description"),
    )
    parser.add_argument("--data", type=Path, help=strings.text("terminal.data_help"))
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help=strings.text("cli.status_help"))
    status.add_argument("--json", action="store_true", dest="as_json")

    start = commands.add_parser("start", help=strings.text("cli.start_help"))
    start.add_argument("phase", nargs="?", type=phase_argument)
    start.add_argument("-m", "--minutes", type=int)
    start.add_argument("--json", action="store_true", dest="as_json")

    for action in ("pause", "resume", "finish", "cancel", "clear"):
        command = commands.add_parser(
            action,
            help=strings.text("cli.action_help", action=strings.text(f"action.{action}")),
        )
        command.add_argument("--json", action="store_true", dest="as_json")

    history = commands.add_parser("history", help=strings.text("cli.history_help"))
    history.add_argument("-n", "--limit", type=int, default=10)
    history.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print_state(state: dict[str, Any], stream: TextIO, strings: Strings) -> None:
    display = state.get("display", state)
    print(
        strings.text(
            "cli.state",
            phase=strings.text(f"phase.{display['phase']}"),
            status=strings.text(f"status.{state['status']}"),
            remaining=display["remaining"],
        ),
        file=stream,
    )
    if display["taskTitle"]:
        print(strings.text("terminal.task", task=display["taskTitle"]), file=stream)
    for key, count in (
        ("terminal.pending_commands", state["pendingCommands"]),
        ("terminal.pending_durations", state["pendingDurationOperations"]),
        ("terminal.pending_auto_start", state["pendingAutoStartOperations"]),
    ):
        if count:
            print(strings.plural(key, count), file=stream)
    if state["historyResolutionPending"]:
        print(strings.text("terminal.history_resolution_pending"), file=stream)


def _history_time(value: str | None, strings: Strings | None = None) -> str:
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


def run(
    args: argparse.Namespace, timer: LocalTimer, stream: TextIO, strings: Strings | None = None
) -> None:
    strings = strings or Strings()
    if args.command == "history":
        if args.limit < 1:
            raise InvalidAction(strings.text("error.history_limit"))
        history = timer.completed_history(args.limit)
        if args.as_json:
            print(json.dumps(history, indent=2), file=stream)
            return
        if not history:
            print(strings.text("terminal.history_empty"), file=stream)
            return
        for item in history:
            phase = strings.text(f"phase.{item.get('phase', 'timer')}")
            if item.get("taskTitle"):
                phase = strings.text(
                    "cli.phase_task", phase=phase, task=item["taskTitle"]
                )
            minutes = int(item.get("plannedDurationMs", 0)) // 60_000
            when = _history_time(item.get("completedAt") or item.get("endedAt"), strings)
            print(
                strings.text(
                    "cli.history_row",
                    when=when,
                    phase=phase,
                    minutes=minutes,
                    pending=(
                        strings.text("terminal.pending_suffix")
                        if item.get("pending")
                        else ""
                    ),
                ),
                file=stream,
            )
        return

    previous = timer.current_timer()
    state = timer.state(auto_finish=True)
    auto_transitioned = (
        state["timerId"] != (previous.get("id") or None)
        or state["status"] != previous.get("status")
    )
    if args.command == "start" and not auto_transitioned:
        timer.issue("start", phase=args.phase, minutes=args.minutes)
    elif args.command != "status" and not auto_transitioned:
        timer.issue(args.command)

    state = timer.state(auto_finish=True)
    if args.as_json:
        print(json.dumps(state, indent=2), file=stream)
    else:
        _print_state(state, stream, strings)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Store | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    locale: str | None = None,
) -> int:
    strings = Strings(locale)
    parser = build_parser(strings)
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    owns_store = store is None
    store = store or Store(args.data.expanduser() if args.data else None)
    try:
        run(args, LocalTimer(store, strings=strings), stdout, strings)
        return 0
    except (InvalidAction, OSError) as error:
        print(strings.text("cli.error", error=error), file=stderr)
        return 2
    finally:
        if owns_store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
