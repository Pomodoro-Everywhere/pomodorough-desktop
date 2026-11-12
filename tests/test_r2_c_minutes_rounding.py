"""UX round 2 batch C follow-up F1: one minutes rule for rows and task totals.

Elapsed minutes round half up, and any run above 0 shows at least 1 minute,
so a 29 s run reads "Finished early · 1 min". A row with elapsedMs 0 shows
"0 min". The Arrivals rows, the CLI and terminal history, and the Tasks
screen time column and totals all use the same rule.
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.arrival_minutes_wording import arrival_minutes
from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.cli import _print_history
from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen
from pomodorough.tui import _history_lines
from test_r2_c_history_elapsed import FINISHED_EARLY, LOCALES, MINUTES, TASK, TASKS, _row, _rows

# (elapsedMs, shown minutes). 29 s and 30 s are the lead's cases; 7 min 30 s
# is where the old task total floored to 7.
ROUNDING = [
    (0, 0),
    (1, 1),
    (29_000, 1),
    (29_999, 1),
    (30_000, 1),
    (89_999, 1),
    (90_000, 2),
    (449_999, 7),
    (450_000, 8),
]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize(("elapsed_ms", "minutes"), ROUNDING)
def test_arrival_minutes_round_half_up_with_a_one_minute_floor(elapsed_ms, minutes):
    shown = arrival_minutes(_row(elapsedMs=elapsed_ms))
    assert (shown.status_key, shown.minutes) == ("arrivals.status.finished_early", minutes)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(("elapsed_ms", "minutes"), [(29_000, 1), (30_000, 1), (0, 0)])
def test_a_short_early_finish_row_never_reads_as_zero(app, locale, elapsed_ms, minutes):
    screen = ArrivalsScreen(Strings(locale), "device-12345678")
    screen.render([_row(elapsedMs=elapsed_ms)], TASKS)
    (text,) = _rows(screen)
    assert f"{FINISHED_EARLY[locale]}  {MINUTES[locale].format(minutes)}" in text


def test_cli_and_terminal_rows_use_the_same_rule():
    class Timer:
        def retained_history(self, _limit: int) -> list[dict[str, Any]]:
            return [_row(elapsedMs=29_000)]

    stream = io.StringIO()
    _print_history(argparse.Namespace(limit=5, as_json=False), Timer(), stream, Strings("en"))
    assert "| Finished early | 1 min" in stream.getvalue()
    (line,) = _history_lines([_row(elapsedMs=29_000)], 80, Strings("en"))
    assert "| Finished early | 1 min" in line


def _task_time(elapsed_ms: int, **row: Any) -> tuple[str, str]:
    """Render one task with one finished row; return its cell and the totals."""
    screen = TasksScreen(Strings("en"))
    try:
        history = [_row(elapsedMs=elapsed_ms, **row)]
        screen.render([TASK], history, mutations_enabled=True)
        return screen.task_table.item(0, 2).text(), screen.task_totals.text()
    finally:
        screen.deleteLater()


@pytest.mark.parametrize(("elapsed_ms", "minutes"), ROUNDING)
def test_task_time_and_totals_use_the_row_rule(app, elapsed_ms, minutes):
    cell, totals = _task_time(elapsed_ms)
    expected = Strings("en").text("duration.minutes", minutes=minutes)
    assert cell == expected
    assert expected.upper() in totals


def test_task_time_rounds_into_hours_with_the_same_rule(app):
    strings = Strings("en")
    # 59 min 30 s rounds up to a whole hour, as a row would show 60 min.
    cell, _totals = _task_time(3_570_000, plannedDurationMs=3_600_000)
    assert cell == strings.text("duration.hours", hours=1)
