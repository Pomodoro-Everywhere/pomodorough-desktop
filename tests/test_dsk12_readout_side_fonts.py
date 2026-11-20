"""R55-D01: the compact readout's side holds its contents at every font.

A font family decides the line height and the letter widths at one point
size. The DSK-12 cases were accepted with the macOS system font; a font
with a taller line (Noto Sans, Nimbus Sans) left the readout less than its
least height above the task picker, and one with wider letters cut the
picker's title. Each case runs here with several font families. A family
the host does not have is skipped.

The invariant holds wherever the controls beside the readout show without
a scroll range. Where they scroll in both forms, the readout may go below
its least height and a stacked picker may cut the title (DSK-12 note H);
that trade waits for the DSK-12 decision about larger fonts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication
from test_dsk12_compact_readout import (
    BASE_POINTS,
    LOCALES,
    _page_would_hold_the_compact_readout,
    _title_cut,
)
from test_r2_c_m3_timer_width import _timer_window
from test_r52_d05_nav_reflow import _process, _scaled_font
from test_ux_r3_timer_flow import SOUND_STATES, _show_state

FONT_FAMILIES = (
    "Noto Sans",
    "DejaVu Sans",
    "Nimbus Sans",
    "Helvetica Neue",
    "Avenir Next",
    "Verdana",
)
WINDOW_SIZES = ("default", "minimum")
FONT_SCALES = (1.5, 1.75, 2.0, 2.5)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _resolved_family(font: QFont) -> str:
    """The family Qt draws with, without the foundry fontconfig appends."""
    return QFontInfo(font).family().split(" [")[0]


@contextmanager
def _font_family(app: QApplication, family: str):
    previous = QFont(QApplication.font())
    font = QFont(previous)
    font.setFamily(family)
    if _resolved_family(font) != family:
        pytest.skip(f"the host has no {family} font")
    QApplication.setFont(font)
    _process(app)
    try:
        yield
    finally:
        QApplication.setFont(previous)
        _process(app)


@contextmanager
def _timer_window_with_font(app, tmp_path, family, scale, locale, size):
    with (
        _font_family(app, family),
        _scaled_font(app, BASE_POINTS * scale),
        _timer_window(app, tmp_path, locale, size) as window,
    ):
        yield window


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", FONT_SCALES)
@pytest.mark.parametrize("size", WINDOW_SIZES)
@pytest.mark.parametrize("family", FONT_FAMILIES)
def test_compact_readout_side_is_whole_with_each_font_family(
    app, tmp_path, family, size, scale, locale
):
    with _timer_window_with_font(app, tmp_path, family, scale, locale, size) as window:
        screen = window.timer_screen
        bar = screen.controls_scroll.verticalScrollBar()
        for label, state in SOUND_STATES.items():
            _show_state(app, window, state)
            if not screen.clock.compact_readout or bar.maximum() > 0:
                continue
            clock = screen.clock
            assert clock.height() >= clock.readout_minimum().height(), label
            assert _title_cut(screen.task_combo) <= 0, label


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("scale", FONT_SCALES)
@pytest.mark.parametrize("size", WINDOW_SIZES)
@pytest.mark.parametrize("family", FONT_FAMILIES)
def test_round_dial_stays_only_where_the_page_cannot_hold_the_compact_readout(
    app, tmp_path, family, size, scale, locale
):
    with _timer_window_with_font(app, tmp_path, family, scale, locale, size) as window:
        screen = window.timer_screen
        if screen.clock.compact_readout:
            return
        bar = screen.controls_scroll.verticalScrollBar()
        scrolls = False
        for state in SOUND_STATES.values():
            _show_state(app, window, state)
            scrolls = scrolls or bar.maximum() > 0
    if not scrolls:
        # The round dial fits the page: nothing asks for the swap.
        return
    # A new window measures the buttons as the form's choice did. The
    # states shown above measured them again, and a button measured with
    # the focus ring asks for a few more pixels than one measured at rest.
    with _timer_window_with_font(app, tmp_path, family, scale, locale, size) as window:
        assert not window.timer_screen.clock.compact_readout
        assert not _page_would_hold_the_compact_readout(app, window)
