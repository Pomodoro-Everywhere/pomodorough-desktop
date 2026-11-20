from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from .arrival_minutes_wording import arrival_minutes
from .arrival_time_wording import arrival_ended_at
from .core import BREAK_PHASES
from .localization import Strings
from .sentry_monitoring import capture_exception, init_sentry_from_environment
from .shared_core import SharedCoreError
from .storage import Store
from .terminal import InvalidAction, LocalTimer, normalize_phase

JSON_ERROR_VERSION = 1
STORAGE_ERRORS = (OSError, sqlite3.Error, SharedCoreError)


class CLIArgumentError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser


class CLIArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIArgumentError(self, message)


HISTORY_LIMIT_DEFAULT = 10


def phase_argument(value: str) -> str:
    try:
        return normalize_phase(value)
    except InvalidAction as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser(strings: Strings | None = None) -> argparse.ArgumentParser:
    strings = strings or Strings()
    parser = CLIArgumentParser(
        prog="pomodorough-cli",
        description=strings.text("cli.description"),
    )
    parser.add_argument("--data", type=Path, help=strings.text("terminal.data_help"))
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help=strings.text("cli.status_help"))
    _add_json_argument(status, strings)

    start = commands.add_parser("start", help=strings.text("cli.start_help"))
    start.add_argument(
        "phase", nargs="?", type=phase_argument, help=strings.text("cli.phase_help")
    )
    start.add_argument(
        "-m", "--minutes", type=int, help=strings.text("cli.minutes_help")
    )
    _add_json_argument(start, strings)

    for action in ("pause", "resume", "finish", "cancel"):
        command = commands.add_parser(
            action,
            help=strings.text("cli.action_help", action=strings.text(f"action.{action}")),
        )
        _add_json_argument(command, strings)

    dismiss = commands.add_parser(
        "dismiss",
        aliases=("clear",),
        help=strings.text(
            "cli.action_help", action=strings.text("action.dismiss")
        ),
    )
    dismiss.set_defaults(timer_action="dismiss")
    _add_json_argument(dismiss, strings)

    history = commands.add_parser("history", help=strings.text("cli.history_help"))
    history.add_argument(
        "-n",
        "--limit",
        type=int,
        default=HISTORY_LIMIT_DEFAULT,
        help=strings.text("cli.limit_help", default=HISTORY_LIMIT_DEFAULT),
    )
    _add_json_argument(history, strings)
    return parser


def _add_json_argument(command: argparse.ArgumentParser, strings: Strings) -> None:
    command.add_argument(
        "--json", action="store_true", dest="as_json", help=strings.text("cli.json_help")
    )


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
    if display["taskTitle"] and display.get("phase") not in BREAK_PHASES:
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


def _print_json_error(
    *, code: str, error_type: str, message: str, stream: TextIO
) -> None:
    print(
        json.dumps(
            {
                "version": JSON_ERROR_VERSION,
                "code": code,
                "type": error_type,
                "message": message,
            }
        ),
        file=stream,
    )


def run(
    args: argparse.Namespace, timer: LocalTimer, stream: TextIO, strings: Strings | None = None
) -> None:
    strings = strings or Strings()
    if args.command == "history":
        _print_history(args, timer, stream, strings)
        return
    state = _state_after_command(args, timer)
    if args.as_json:
        print(json.dumps(state, indent=2), file=stream)
    else:
        _print_state(state, stream, strings)


def _print_history(
    args: argparse.Namespace,
    timer: LocalTimer,
    stream: TextIO,
    strings: Strings,
) -> None:
    if args.limit < 1:
        raise InvalidAction(strings.text("error.history_limit"))
    history = timer.retained_history(args.limit)
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
        wording = arrival_minutes(item)
        print(
            strings.text(
                (
                    "cli.history_row_planned"
                    if wording.reads_as_planned
                    else "cli.history_row"
                ),
                when=arrival_ended_at(item, strings),
                phase=phase,
                status=strings.text(wording.status_key),
                minutes=wording.minutes,
                pending=(
                    strings.text("terminal.pending_suffix")
                    if item.get("pending")
                    else ""
                ),
            ),
            file=stream,
        )


def _state_after_command(
    args: argparse.Namespace, timer: LocalTimer
) -> dict[str, Any]:
    previous = timer.current_timer()
    state = timer.state(auto_finish=True)
    auto_transitioned = (
        state["timerId"] != (previous.get("id") or None)
        or state["status"] != previous.get("status")
    )
    if args.command == "start" and not auto_transitioned:
        timer.issue("start", phase=args.phase, minutes=args.minutes)
    elif args.command != "status" and not auto_transitioned:
        timer.issue(getattr(args, "timer_action", args.command))
    return timer.state(auto_finish=True)


def _parse_arguments(
    raw_args: tuple[str, ...], strings: Strings, stderr: TextIO
) -> argparse.Namespace | None:
    parser = build_parser(strings)
    try:
        return parser.parse_args(raw_args)
    except CLIArgumentError as error:
        if "--json" in raw_args:
            _print_json_error(
                code="invalid_arguments",
                error_type="ArgumentError",
                message=str(error),
                stream=stderr,
            )
        else:
            error.parser.print_usage(stderr)
            print(
                strings.text(
                    "cli.argument_error",
                    program=error.parser.prog,
                    error=error,
                ),
                file=stderr,
            )
        return None


def _run_with_store(
    args: argparse.Namespace,
    store: Store | None,
    output: TextIO,
    strings: Strings,
) -> (
    InvalidAction
    | OSError
    | sqlite3.Error
    | SharedCoreError
    | json.JSONDecodeError
    | KeyError
    | TypeError
    | ValueError
    | None
):
    owns_store = store is None
    runtime_error = None
    try:
        store = store or Store(args.data.expanduser() if args.data else None)
        run(args, LocalTimer(store, strings=strings), output, strings)
    except STORAGE_ERRORS as error:
        # Sweep: infra failure reports to Sentry; CLI still prints + exits 2.
        # D63: SharedCoreError (wasm load) is infra, not validation:
        # _default_shared_core() constructs outside the storage try, so it
        # escapes as SharedCoreError and must capture here, never traceback.
        capture_exception(error)
        runtime_error = error
    except (InvalidAction, KeyError, TypeError, ValueError) as error:
        # D59: validation stays silent (no Sentry capture), like the TUI
        # loop; json.JSONDecodeError is a ValueError subclass, covered here.
        runtime_error = error
    finally:
        if owns_store and store is not None:
            try:
                store.close()
            except STORAGE_ERRORS as error:
                # Sweep: close-path infra reports too; first error still wins.
                capture_exception(error)
                runtime_error = runtime_error or error
    return runtime_error


def _print_runtime_error(
    error: (
        InvalidAction
        | OSError
        | sqlite3.Error
        | SharedCoreError
        | json.JSONDecodeError
        | KeyError
        | TypeError
        | ValueError
    ),
    as_json: bool,
    stderr: TextIO,
    strings: Strings,
) -> None:
    if as_json:
        if isinstance(error, InvalidAction):
            code, error_type = "invalid_action", "InvalidAction"
        else:
            code, error_type = "storage_error", "StorageError"
        _print_json_error(
            code=code,
            error_type=error_type,
            message=str(error),
            stream=stderr,
        )
    else:
        print(strings.text("cli.error", error=error), file=stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    store: Store | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    locale: str | None = None,
) -> int:
    strings = Strings(locale)
    init_sentry_from_environment()
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    raw_args = tuple(sys.argv[1:] if argv is None else argv)
    args = _parse_arguments(raw_args, strings, stderr)
    if args is None:
        return 2
    output = io.StringIO() if args.as_json else stdout
    runtime_error = _run_with_store(args, store, output, strings)
    if runtime_error is not None:
        _print_runtime_error(runtime_error, args.as_json, stderr, strings)
        return 2
    if args.as_json:
        stdout.write(output.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
