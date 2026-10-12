"""R48-D06: TUI reserves error and Quit help at 40x12 minimum."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pomodorough import tui
from pomodorough.localization import Strings
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from test_tui import FakeScreen, timer_state
from test_ui import MemorySecretStore


def _writes(screen: FakeScreen) -> list[str]:
    return [row[2] for row in screen.writes]


def _shows_quit(screen: FakeScreen) -> bool:
    return any("q" in text and "quit" in text.lower() for text in _writes(screen))


def _real_timer() -> tuple[LocalTimer, TemporaryDirectory, Store]:
    directory = TemporaryDirectory()
    store = Store(Path(directory.name) / "s.sqlite3", iroh_secret_store=MemorySecretStore())
    return LocalTimer(store, strings=Strings("en")), directory, store


def test_small_minimum_shows_error_and_quit() -> None:
    timer, directory, store = _real_timer()
    try:
        screen = FakeScreen(12, 40)
        tui._draw(screen, timer, "Cannot finish an idle timer.")
        assert any("Cannot finish" in text for text in _writes(screen))
        assert _shows_quit(screen)
        assert all(len(text) <= 39 for text in _writes(screen))
    finally:
        store.close()
        directory.cleanup()


def test_full_key_loop_reports_then_quits_at_minimum() -> None:
    timer, directory, store = _real_timer()
    try:
        keys = [ord("f"), ord(" "), ord("x"), ord("c"), ord("1"),
                ord("2"), ord("3"), ord("+"), ord("-"), ord("q")]
        screen = FakeScreen(12, 40, keys)
        with patch.object(tui.curses, "curs_set"):
            tui._run(screen, timer)
        assert screen.keys == []
        assert screen.refreshes >= 2
        assert any("Cannot" in text or "cannot" in text.lower() for text in _writes(screen))
        assert _shows_quit(screen)
    finally:
        store.close()
        directory.cleanup()


def test_pending_task_state_keeps_error_and_quit() -> None:
    state = timer_state(taskTitle="Write tests", pendingCommands=2,
                        pendingDurationOperations=3, pendingAutoStartOperations=4,
                        historyResolutionPending=True, status="running")
    state["display"] = {"phase": "focus", "taskTitle": "Write tests",
                        "progress": 0.5, "remaining": "12:30"}
    timer = Mock()
    timer.state.return_value = state
    timer.retained_history.return_value = []
    screen = FakeScreen(12, 40)
    tui._draw(screen, timer, "Cannot pause now", Strings("en"))
    assert any("Cannot pause" in text for text in _writes(screen))
    assert _shows_quit(screen)


def test_long_error_wraps_without_hiding_quit() -> None:
    timer = Mock()
    timer.state.return_value = timer_state()
    timer.retained_history.return_value = []
    long_error = "Cannot finish an idle timer. " + "x " * 60 + "token12345"
    screen = FakeScreen(12, 40)
    tui._draw(screen, timer, long_error, Strings("en"))
    assert any("token12345" in text for text in _writes(screen))
    assert any("Cannot finish" in text for text in _writes(screen))
    assert _shows_quit(screen)


def test_rtl_minimum_shows_error_and_quit() -> None:
    timer = Mock()
    timer.state.return_value = timer_state()
    timer.retained_history.return_value = []
    screen = FakeScreen(12, 40)
    tui._draw(screen, timer, "Cannot finish an idle timer.", Strings("ar-XB"))
    assert any("Cannot finish" in text for text in _writes(screen))
    assert any("q" in text for text in _writes(screen))
    assert any("\u203b" in text or "\u27e6" in text for text in _writes(screen))


def test_resize_keeps_feedback_and_quit() -> None:
    timer, directory, store = _real_timer()
    try:
        screen = FakeScreen(12, 40)
        tui._draw(screen, timer, "Cannot finish an idle timer.")
        assert any("Cannot finish" in text for text in _writes(screen))
        assert _shows_quit(screen)
        screen.height, screen.width = 24, 80
        screen.writes.clear()
        tui._draw(screen, timer, "Cannot finish an idle timer.")
        assert any("Cannot finish" in text for text in _writes(screen))
        assert _shows_quit(screen)
    finally:
        store.close()
        directory.cleanup()


def test_normal_size_shows_full_help_and_error() -> None:
    timer, directory, store = _real_timer()
    try:
        screen = FakeScreen(24, 80)
        tui._draw(screen, timer, "Cannot finish an idle timer.")
        assert any("Cannot finish" in text for text in _writes(screen))
        assert any("space start/pause/resume" in text for text in _writes(screen))
        assert any("q quit" in text for text in _writes(screen))
    finally:
        store.close()
        directory.cleanup()


def test_failure_fallback_shows_quit() -> None:
    screen = FakeScreen(12, 40)
    tui._draw_failure(screen, OSError("disk gone " + "y " * 40), Strings("en"))
    assert any("disk gone" in text for text in _writes(screen))
    assert _shows_quit(screen)
