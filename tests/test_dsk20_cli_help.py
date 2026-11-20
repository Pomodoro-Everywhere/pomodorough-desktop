"""DSK-20: every `pomodorough-cli` subcommand argument has help text.

The audit found no help for the start phase, -m/--minutes, --json and
history -n/--limit, and the phase names were not listed anywhere in help.
"""

from __future__ import annotations

import argparse

import pytest

from pomodorough.cli import build_parser
from pomodorough.localization import Strings
from pomodorough.terminal import PHASE_ALIASES


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return dict(action.choices)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_every_subcommand_argument_has_help(locale: str) -> None:
    missing = [
        f"{name} {action.dest}"
        for name, command in _subcommands(build_parser(Strings(locale))).items()
        for action in command._actions
        if not action.help
    ]

    assert not missing


def test_start_help_lists_every_phase_name() -> None:
    start = _subcommands(build_parser(Strings("en")))["start"]
    text = " ".join(start.format_help().split())

    for name in sorted(set(PHASE_ALIASES) - {"short_break", "long_break"}):
        assert name in text, name


def test_history_help_states_the_default_limit() -> None:
    history = _subcommands(build_parser(Strings("en")))["history"]

    assert "(default: 10)" in " ".join(history.format_help().split())
    assert history.parse_args([]).limit == 10
