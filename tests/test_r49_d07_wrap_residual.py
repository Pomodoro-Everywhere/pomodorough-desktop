"""R49-D07 residual: wrap boundaries, emoji one-cell fallback, control width."""

from __future__ import annotations

import pytest
from wcwidth import wcswidth

from pomodorough import tui


def test_exact_wrap_boundary_splits_at_usable_width():
    assert tui._wrap_line("x" * 39, 40) == ["x" * 39]
    rows = tui._wrap_line("x" * 40, 40)
    assert "".join(rows) == "x" * 40
    assert len(rows) == 2
    assert all(wcswidth(row) <= 39 for row in rows)
    wide = "界" * 20
    assert tui._wrap_line(wide, 40) == [wide[:19], wide[19:]]


def test_emoji_sequences_stay_whole_and_fallback_consumes_grapheme():
    for cluster in ("👩\u200d💻", "👍🏽", "🇺🇸"):
        rows = tui._wrap_line(cluster * 40, 40)
        assert "".join(rows) == cluster * 40
    assert tui._wrap_line("👩\u200d💻", 2) == ["?"]
    assert tui._wrap_line("\u200d", 40) == ["?"]
    assert tui._wrap_line("\u0301", 40) == ["?"]


@pytest.mark.parametrize("raw", ["a\x07b", "a\x1bb", "a\x00b", "a\x7fb"])
def test_controls_have_safe_width_within_row_budget(raw):
    rows = tui._wrap_line(raw * 40, 40)
    assert "".join(rows) == "a?b" * 40
    assert all(wcswidth(row) <= 39 for row in rows)


@pytest.mark.parametrize("raw", ["a\nb", "a\rb"])
def test_newline_controls_become_spaces_within_budget(raw):
    rows = tui._wrap_line(raw * 40, 40)
    assert "\n" not in "".join(rows) and "\r" not in "".join(rows)
    assert "a b" in rows[0]
    assert all(wcswidth(row) <= 39 for row in rows)


def test_tab_expands_before_measurement():
    rows = tui._wrap_line("a\tb" * 20, 40)
    assert "\t" not in "".join(rows)
    assert all(wcswidth(row) <= 39 for row in rows)
