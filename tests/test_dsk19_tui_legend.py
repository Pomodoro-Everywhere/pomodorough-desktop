"""DSK-19: the key legend takes two rows on a narrow terminal, not four.

At 40 columns the two legend lines wrapped to four rows, with "x" cut from
"cancel", and pushed the arrivals off a 15-row screen.
"""

from __future__ import annotations

import pytest
from test_tui import timer_state

from pomodorough import tui
from pomodorough.localization import Strings

EN = Strings("en")
FULL = (EN.text("tui.keys_primary"), EN.text("tui.keys_secondary"))
NARROW = (EN.text("tui.keys_primary_narrow"), EN.text("tui.keys_secondary_narrow"))
ARRIVAL = {"phase": "focus", "status": "completed", "plannedDurationMs": 1_500_000,
           "completedAt": "2026-07-25T12:34:56Z"}


def _screen(height: int, width: int, strings: Strings = EN) -> list[str]:
    return tui._screen_lines(timer_state(), [ARRIVAL] * 3, height, width, "", strings)


def _legend_rows(height: int, width: int) -> list[str]:
    lines = _screen(height, width)
    start = lines.index(EN.text("tui.keys_quit")) + 1
    return lines[start:lines.index(EN.text("arrivals.title"))]


def test_narrow_terminal_legend_takes_two_whole_rows() -> None:
    assert _legend_rows(15, 40) == list(NARROW)


def test_narrow_terminal_shows_an_arrival_below_the_legend() -> None:
    lines = _screen(15, 40)
    after_title = lines[lines.index(EN.text("arrivals.title")) + 1:]

    assert len(lines) == 15
    assert sum(row.startswith("2026-") for row in after_title) >= 2, lines


@pytest.mark.parametrize("width", range(40, 121))
def test_english_legend_never_wraps(width: int) -> None:
    rows = _legend_rows(24, width)
    fits = all(tui._display_width(line) <= width - 1 for line in FULL)

    assert rows == list(FULL if fits else NARROW)
    assert all(tui._display_width(row) <= width - 1 for row in rows)


def test_full_legend_returns_as_soon_as_it_fits() -> None:
    widest = max(tui._display_width(line) for line in FULL)

    assert _legend_rows(24, widest + 1) == list(FULL)
    assert _legend_rows(24, widest) == list(NARROW)


@pytest.mark.parametrize("hint", ["space", "f finish", "x cancel", "c dismiss",
                                  "1/2/3", "+/-"])
def test_narrow_legend_names_every_key(hint: str) -> None:
    assert hint in "  ".join(NARROW)


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_legend_wording_comes_from_the_catalog(locale: str) -> None:
    strings = Strings(locale)
    rendered = "".join(_screen(40, 200, strings))
    narrow = "".join(_screen(40, 40, strings)).replace(" ", "")

    assert strings.text("tui.keys_primary") in rendered
    assert strings.text("tui.keys_secondary") in rendered
    assert strings.text("tui.keys_primary_narrow").replace(" ", "") in narrow
    assert strings.text("tui.keys_secondary_narrow").replace(" ", "") in narrow


LONG_SECOND_LINE = {
    "tui.keys_primary": "space start",
    "tui.keys_secondary": "f finish  x cancel  c dismiss",
    "tui.keys_primary_narrow": "spc",
    "tui.keys_secondary_narrow": "f x c",
}


class _LongSecondLine:
    """A catalog whose second legend line is the wider one."""

    def text(self, key: str) -> str:
        return LONG_SECOND_LINE[key]


def test_narrow_legend_follows_the_wider_second_line() -> None:
    # In the shipped catalogs the first line is the wider one. A translation
    # may differ, so the second line must count on its own.
    strings = _LongSecondLine()
    first = strings.text("tui.keys_primary")
    second = strings.text("tui.keys_secondary")
    width = tui._display_width(first) + 2

    assert tui._display_width(second) > width
    assert tui._legend_lines(width, strings) == ["spc", "f x c"]
