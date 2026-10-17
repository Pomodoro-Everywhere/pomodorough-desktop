"""R49-D07: oversized content cannot displace feedback or localized Quit help."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from pomodorough import tui
from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from wcwidth import iter_graphemes, wcswidth
from test_r43_d05_tui_failure_loop import PacedScreen
from test_r43_d05_tui_failure_loop import loop_environment as loop_environment
from test_tui import FakeScreen, timer_state


MAXIMUM_TITLES = ["task " * 102 + "ok", "x" * 512, "界" * 170 + "ok"]
OVERFLOW_ERRORS = [
    "Cannot pause now. " + "failure " * 200 + "END",
    "Cannot pause now. " + "x" * 2000 + "END",
    "Cannot pause now. " + "界" * 1000 + "END",
]


def _cells(text: str) -> int:
    # Known widths for this fixture alphabet, independent of production Unicode logic.
    widths = {"界": 2, "🙂": 2, "\u0301": 0}
    return sum(widths.get(char, 1) for char in text)


class CellScreen(PacedScreen):
    def addnstr(self, row, column, text, limit, *args):
        visible = text[:limit]
        assert _cells(visible) <= self.width - 1
        assert 0 <= row < self.height
        FakeScreen.addnstr(self, row, column, text, limit, *args)
        self.frame.append(visible)


@pytest.fixture
def running_timer(tmp_path):
    store = Store(tmp_path / "timer.sqlite3")
    timer = LocalTimer(store, strings=Strings("en"))
    try:
        yield timer
    finally:
        store.close()


def _start_task(timer, title):
    task = task_from_title(title)
    assert len(task["title"].encode("utf-8")) == 512
    timer.store.queue_task_operation("upsert", task)
    timer.store.set_selected_task_id(task["id"])
    timer.issue("start")
    state = timer.state()
    assert state["display"]["taskTitle"] == title
    assert state["status"] == "running"


def _assert_feedback_and_quit(frame, strings, feedback="Cannot pause now"):
    assert feedback in frame
    assert "q " + strings.text("tui.keys_secondary").split("q ", 1)[1] in frame


@pytest.mark.parametrize("title", MAXIMUM_TITLES, ids=["words", "unbroken", "wide"])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_maximum_valid_task_reserves_feedback_and_localized_quit(
    running_timer, title, locale,
):
    _start_task(running_timer, title)
    strings = Strings(locale)
    running_timer.strings = strings
    screen = CellScreen([], shape=(12, 40))
    before = running_timer.store.load()

    tui._draw(screen, running_timer, "Cannot pause now")

    _assert_feedback_and_quit(screen.frames[-1], strings)
    assert len(screen.frame) <= 12
    assert running_timer.store.load() == before


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("error", OVERFLOW_ERRORS, ids=["words", "unbroken", "wide"])
def test_error_overflow_is_bounded_and_keeps_localized_quit(fallback, locale, error):
    strings = Strings(locale)
    screen = CellScreen([], shape=(12, 40))
    timer = Mock()
    timer.state.return_value = timer_state(taskTitle="x" * 512)
    timer.retained_history.return_value = []

    if fallback:
        tui._draw_failure(screen, OSError(error), strings)
        assert not timer.mock_calls
    else:
        tui._draw(screen, timer, error, strings)

    _assert_feedback_and_quit(screen.frames[-1], strings)
    assert "END" not in screen.frames[-1]
    assert "…" in screen.frames[-1]
    feedback_start = next(index for index, row in enumerate(screen.frame)
                          if row.startswith("Cannot pause now"))
    feedback_end = next(index for index, row in enumerate(screen.frame)
                        if row.endswith("…"))
    assert feedback_end - feedback_start + 1 <= (11 if fallback else 6)
    assert len(screen.frame) <= 12


@pytest.mark.parametrize("title", MAXIMUM_TITLES, ids=["words", "unbroken", "wide"])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_roomy_screen_keeps_pending_history_full_help_and_title(
    running_timer, title, locale,
):
    _start_task(running_timer, title)
    running_timer.strings = Strings(locale)
    running_timer.store.prepare_resolution({"id": "user-1"}, 4, "merge")
    strings = running_timer.strings
    screen = CellScreen([], shape=(40, 80))
    before = running_timer.store.load()

    tui._draw(screen, running_timer, "Cannot pause now")

    frame = screen.frames[-1]
    _assert_feedback_and_quit(frame, strings)
    assert title.replace(" ", "") in "".join(screen.frame).replace(" ", "")
    assert strings.plural("terminal.pending_commands", 1) in frame
    assert strings.text("terminal.history_resolution_pending") in frame
    assert strings.text("tui.keys_primary") in frame
    assert strings.text("tui.keys_secondary") in frame
    assert strings.text("arrivals.title") in frame
    assert strings.text("brand.tagline") in frame
    assert running_timer.store.load() == before


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("message", ["Cannot pause now", OVERFLOW_ERRORS[0]],
                         ids=["short-error", "overflow"])
def test_resize_restores_optional_content_then_preserves_feedback(
    running_timer, locale, message,
):
    _start_task(running_timer, MAXIMUM_TITLES[1])
    running_timer.strings = Strings(locale)
    screen = CellScreen([], shape=(12, 40))
    for height, width in [(12, 40), (40, 80), (12, 40)]:
        screen.height, screen.width = height, width
        tui._draw(screen, running_timer, message)
        _assert_feedback_and_quit(screen.frames[-1], running_timer.strings)
        assert len(screen.frame) <= height
        if height == 40 and message == "Cannot pause now":
            assert running_timer.strings.text("tui.keys_primary") in screen.frames[-1]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_roomy_error_keeps_full_message_and_help(fallback, locale):
    strings = Strings(locale)
    message = "Cannot pause now. " + "detail " * 30 + "END"
    screen = CellScreen([], shape=(40, 80))
    if fallback:
        tui._draw_failure(screen, OSError(message), strings)
    else:
        timer = Mock()
        timer.state.return_value = timer_state()
        timer.retained_history.return_value = []
        tui._draw(screen, timer, message, strings)
        assert strings.text("tui.keys_primary") in screen.frames[-1]
    assert message in " ".join(screen.frame)
    assert strings.text("tui.keys_secondary") in screen.frames[-1]
    assert "…" not in screen.frames[-1]


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("width", [40, 54, 62, 80])
def test_pending_full_key_loop_keeps_every_notice_and_quits(
    running_timer, loop_environment, monkeypatch, locale, width,
):
    _start_task(running_timer, MAXIMUM_TITLES[1])
    running_timer.strings = Strings(locale)
    running_timer.store.prepare_resolution({"id": "user-1"}, 4, "merge")
    keys = [-1] + [ord(key) for key in " fFxXcC123+=-_?"] + [ord("Q")]
    screen = CellScreen(keys, shape=(12, width))
    captures = loop_environment(screen)
    before = running_timer.store.load()
    original = tui.handle_key
    failures = []

    def handle_key(timer, key):
        try:
            return original(timer, key)
        except tui.NOTICE_ERRORS as error:
            failures.append((screen.reads, str(error)))
            raise

    monkeypatch.setattr(tui, "handle_key", handle_key)

    tui._run(screen, running_timer)

    assert screen.reads == len(keys)
    assert screen.keys == []
    assert screen.timeout_ms == 250
    assert not captures
    assert all("q " in frame for frame in screen.frames)
    assert len(failures) == 14
    for read, notice in failures:
        assert " ".join(notice.split()) in " ".join(screen.frames[read].split())
    assert running_timer.store.load() == before


def test_long_task_full_key_loop_applies_actions_and_quits(running_timer, loop_environment):
    _start_task(running_timer, MAXIMUM_TITLES[1])
    keys = [ord(key) for key in "  fc231+- xq"]
    screen = CellScreen(keys, shape=(12, 40))
    captures = loop_environment(screen)

    tui._run(screen, running_timer)

    assert screen.reads == len(keys)
    assert screen.keys == []
    assert all("q quit" in frame for frame in screen.frames)
    assert not captures
    assert [command["type"] for command in running_timer.store.load()["pending"]] == [
        "start", "pause", "resume", "finish", "clear", "start", "cancel", "clear",
    ]
    assert running_timer.state()["status"] == "idle"
    assert running_timer.selected_phase == "focus"
    assert running_timer.settings["durationsMs"]["focus"] == 25 * 60_000


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_overflowing_failure_loop_keeps_feedback_quit_and_pacing(loop_environment, locale):
    strings = Strings(locale)
    screen = CellScreen([-1, -1, ord("q")], shape=(12, 40))
    captures = loop_environment(screen)
    timer = Mock()
    timer.strings = strings
    timer.state.side_effect = OSError(OVERFLOW_ERRORS[0])

    tui._run(screen, timer)

    assert screen.reads == timer.state.call_count == 3
    assert screen.keys == []
    assert screen.now == 0.5
    assert len(captures) == 1
    timer.retained_history.assert_not_called()
    for frame in screen.frames:
        _assert_feedback_and_quit(frame, strings)


@pytest.mark.parametrize("text, expected", [
    ("界" * 40, ["界" * 19, "界" * 19, "界" * 2]),
    ("e\u0301" * 40, ["e\u0301" * 39, "e\u0301"]),
    ("🙂" * 40, ["🙂" * 19, "🙂" * 19, "🙂" * 2]),
], ids=["wide", "combining", "emoji"])
def test_wrap_uses_display_cells_without_splitting_combining_marks(text, expected):
    assert tui._wrap_line(text, 40) == expected
    assert "".join(tui._wrap_line(text, 40)) == text


@pytest.mark.parametrize("fallback", [False, True])
def test_draw_preserves_combining_text_beyond_character_limit(fallback):
    message = "e\u0301" * 39
    screen = CellScreen([], shape=(12, 40))
    if fallback:
        tui._draw_failure(screen, OSError(message), Strings("en"))
    else:
        timer = Mock()
        timer.state.return_value = timer_state()
        timer.retained_history.return_value = []
        tui._draw(screen, timer, message, Strings("en"))
    assert message in screen.frame
    assert "q quit" in screen.frames[-1]


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_all_supported_resize_widths_keep_complete_quit(running_timer, locale):
    _start_task(running_timer, MAXIMUM_TITLES[1])
    running_timer.strings = Strings(locale)
    running_timer.store.prepare_resolution({"id": "user-1"}, 4, "merge")
    before = "\n".join(running_timer.store.connection.iterdump())
    for width in range(40, 81):
        screen = CellScreen([], shape=(12, width))
        tui._draw(screen, running_timer, "Cannot pause now")
        _assert_feedback_and_quit(screen.frames[-1], running_timer.strings)
        tui._draw_failure(screen, OSError(OVERFLOW_ERRORS[0]), running_timer.strings)
        _assert_feedback_and_quit(screen.frames[-1], running_timer.strings)
    assert "\n".join(running_timer.store.connection.iterdump()) == before


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_roomy_21_row_error_is_not_truncated(running_timer, fallback, locale):
    strings = Strings(locale)
    running_timer.strings = strings
    message = "Cannot pause now. " + "detail " * 225 + "END"
    screen = CellScreen([], shape=(40, 80))
    before = "\n".join(running_timer.store.connection.iterdump())
    assert len(tui._wrap_line(message, 80)) == 21
    if fallback:
        tui._draw_failure(screen, OSError(message), strings)
    else:
        tui._draw(screen, running_timer, message)
    assert message in " ".join(screen.frame)
    assert "…" not in screen.frames[-1]
    _assert_feedback_and_quit(screen.frames[-1], strings)
    assert "\n".join(running_timer.store.connection.iterdump()) == before


@pytest.mark.parametrize("cluster,cells", [
    ("👩\u200d💻", 4), ("👍🏽", 4), ("🇺🇸", 4),
    ("❤️", 2), ("1️⃣", 2), ("❤\ufe0e", 1),
], ids=["zwj", "modifier", "flag", "vs16", "keycap", "vs15"])
def test_grapheme_wrap_and_ellipsis_keep_complete_sequences(cluster, cells):
    rows = tui._wrap_line(cluster * 40, 40)
    assert tui._display_width(cluster) == cells
    assert "".join(rows) == cluster * 40
    assert all(set(iter_graphemes(row)) == {cluster} for row in rows)
    assert all(wcswidth(row) <= 39 for row in rows)
    assert tui._wrap_line(cluster, 2) == (["?"] if cells > 1 else [cluster])
    feedback = tui._feedback_lines(cluster * 40, 1, 40)
    assert feedback[0].endswith("…")
    assert set(iter_graphemes(feedback[0][:-1])) == {cluster}


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x1b", "\x7f", "\x85", "\u200e", "\u202e"])
def test_display_controls_are_replaced_before_measurement(control):
    rows = tui._wrap_line(("a" + control + "b") * 40, 40)
    assert "".join(rows) == "a?b" * 40
    assert all(wcswidth(row) <= 39 for row in rows)


def test_control_sanitization_covers_c0_c1_and_surrogates():
    controls = "".join(chr(value) for value in range(32))
    controls += "".join(chr(value) for value in range(127, 160)) + "\ud800"
    safe = tui._display_text(controls)
    assert all(char in " ?" for char in safe)
    assert safe.count("?") == 61
    assert tui._wrap_line("\u200d", 40) == ["?"]
    assert tui._wrap_line("\u0301", 40) == ["?"]
