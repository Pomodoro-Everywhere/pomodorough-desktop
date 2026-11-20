"""DSK-19: the terminal UI legend, pending-sync lines and arrival rows.

The audit found a lone "q quit" under the progress bar repeated in the
legend, "command(s) pending sync" jargon with "(s)" despite plural keys,
arrival rows ending in an unexplained " *", and arrival rows without a time.
"""

from __future__ import annotations

import re

import pytest
from test_tui import FakeScreen, timer_state

from pomodorough import tui
from pomodorough.arrival_time_wording import arrival_time
from pomodorough.localization import Strings

LOCALES = ("en", "ar-XB")
QUIT_WORD = {"en": "quit", "ar-XB": "qüït"}
PENDING_KEYS = (
    ("terminal.pending_commands", "pendingCommands"),
    ("terminal.pending_durations", "pendingDurationOperations"),
    ("terminal.pending_auto_start", "pendingAutoStartOperations"),
)


def _screen(strings: Strings, height: int = 24, width: int = 80, message: str = ""):
    return tui._screen_lines(timer_state(), [], height, width, message, strings)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("size", [(24, 80), (15, 40), (50, 200)])
def test_quit_key_shows_once(locale: str, size: tuple[int, int]) -> None:
    strings = Strings(locale)
    lines = _screen(strings, *size, message="Cannot pause now")
    quit_rows = [line for line in lines if QUIT_WORD[locale] in line]

    assert len(quit_rows) == 1, lines


def test_english_legend_names_quit_once() -> None:
    rendered = "\n".join(_screen(Strings("en")))

    assert rendered.count("q quit") == 1, rendered


def test_failure_screen_names_quit_once() -> None:
    screen = FakeScreen(24, 80)

    tui._draw_failure(screen, OSError("disk unavailable"), Strings("en"))
    rendered = "\n".join(write[2] for write in screen.writes)

    assert "disk unavailable" in rendered
    assert rendered.count("q quit") == 1, rendered


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key, _field", PENDING_KEYS)
def test_pending_lines_use_real_plurals(locale: str, key: str, _field: str) -> None:
    strings = Strings(locale)
    one, many = strings.plural(key, 1), strings.plural(key, 2)

    assert "(s)" not in one + many
    assert one.replace("1", "") != many.replace("2", "")


@pytest.mark.parametrize("key, field", PENDING_KEYS)
def test_pending_lines_avoid_queue_jargon(key: str, field: str) -> None:
    lines = tui._pending_lines(timer_state(**{field: 2}), Strings("en"))

    assert lines == [Strings("en").plural(key, 2)]
    for jargon in ("(s)", "command", "operation", "preference"):
        assert jargon not in lines[0]
    assert lines[0].endswith("pending sync")


def _history_row(**item: object) -> str:
    entry = {"phase": "short_break", "status": "completed", "plannedDurationMs": 300_000}
    entry.update(item)
    return tui._history_lines([entry], 80, Strings("en"))[0]


def test_pending_arrival_says_pending_sync() -> None:
    row = _history_row(pending=True, taskTitle="Notes")

    assert not row.endswith("*"), row
    assert row.endswith("| pending sync"), row
    assert "pending" not in _history_row(taskTitle="Notes")


def test_arrival_phase_uses_catalog_casing() -> None:
    row = _history_row(taskTitle="Notes")

    assert f"| {Strings('en').text('phase.short_break')}: Notes |" in row, row


def test_arrival_row_starts_with_its_local_end_time() -> None:
    ended = "2026-07-25T12:34:56Z"
    row = _history_row(completedAt=ended)

    assert row == f"{arrival_time(ended)} | Short break | Completed | 5 min"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", arrival_time(ended))
    assert _history_row(status="cancelled", endedAt=ended).startswith(arrival_time(ended))
    assert _history_row().startswith(Strings("en").text("terminal.unknown_time"))
