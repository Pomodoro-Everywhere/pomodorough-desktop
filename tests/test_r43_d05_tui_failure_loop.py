"""Bounded real-loop regressions: drawing must never starve quit or pacing."""

import sqlite3
import time
from unittest.mock import Mock

import pytest

from pomodorough import tui
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from test_tui import FakeScreen, timer_state


class PacedScreen(FakeScreen):
    def __init__(self, keys, *, shape=(24, 80)):
        super().__init__(*shape, keys)
        self.now = 0.0
        self.reads = 0
        self.frames = []
        self.frame = []
        self.sleeps = []

    def erase(self):
        super().erase()
        self.frame = []

    def addnstr(self, row, column, text, limit, *args):
        assert 0 <= row < self.height
        assert 0 <= column < self.width
        assert 0 < limit < self.width
        super().addnstr(row, column, text, limit, *args)
        self.frame.append(text[:limit])

    def refresh(self):
        super().refresh()
        self.frames.append("\n".join(self.frame))

    def getch(self):
        self.reads += 1
        key = super().getch()
        if key == -1:
            self.now += self.timeout_ms / 1000
        return key

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def loop_environment(monkeypatch):
    captures = []
    monkeypatch.setattr(tui.curses, "curs_set", lambda _: None)
    monkeypatch.setattr(tui, "capture_exception", captures.append)

    def prepare(screen):
        monkeypatch.setattr(time, "monotonic", lambda: screen.now)
        monkeypatch.setattr(time, "sleep", screen.sleep)
        return captures

    return prepare


def bounded_state(timer, screen, failure):
    attempts = []

    def state(**kwargs):
        assert len(attempts) < 40, "draw retries exhausted before queued q was read"
        attempts.append(screen.now)
        raise failure

    timer.state.side_effect = state
    return attempts


@pytest.mark.parametrize("key", [-1, ord("?")])
@pytest.mark.parametrize("failure", [
    sqlite3.ProgrammingError("closed database"), OSError("disk unavailable"),
    tui.SharedCoreError("Core unavailable"), ValueError("bad snapshot"),
    KeyError("durationsMs"), TypeError("bad shape"),
    tui.InvalidAction("not now"), tui.curses.error("draw failed"),
])
def test_persistent_draw_failure_keeps_paced_input(loop_environment, key, failure):
    screen = PacedScreen([key] * 24 + [ord("q")])
    captures = loop_environment(screen)
    timer = Mock()
    attempts = bounded_state(timer, screen, failure)

    tui._run(screen, timer)

    assert screen.reads == len(attempts) == 25
    assert screen.now == pytest.approx(6.0)
    assert all(b - a >= 0.25 for a, b in zip(attempts, attempts[1:]))
    assert len(captures) == (2 if isinstance(failure, tui.STORAGE_ERRORS) else 0)
    assert len(screen.frames) == 25
    assert len(set(screen.frames)) == 1
    assert str(failure) in screen.frames[0]
    assert "q quit" in screen.frames[0]


def test_closed_sqlite_real_timer_can_quit(tmp_path, loop_environment, monkeypatch):
    store = Store(tmp_path / "closed.sqlite3")
    timer = LocalTimer(store)
    store.close()
    screen = PacedScreen([-1, -1, ord("q")])
    captures = loop_environment(screen)
    original = timer.state
    attempts = []

    def state(**kwargs):
        assert len(attempts) < 8, "closed SQLite prevented queued q"
        attempts.append(screen.now)
        return original(**kwargs)

    monkeypatch.setattr(timer, "state", state)
    tui._run(screen, timer)

    assert screen.reads == len(attempts) == 3
    assert screen.now == 0.5
    assert len(captures) == 1
    assert isinstance(captures[0], sqlite3.ProgrammingError)
    assert "closed database" in screen.frames[-1]


def test_recovery_clears_draw_notice_and_restores_actions(loop_environment):
    screen = PacedScreen([-1, ord(" "), ord("q")])
    captures = loop_environment(screen)
    timer = Mock()
    timer.state.side_effect = [OSError("disk unavailable"), timer_state(), timer_state()]
    timer.retained_history.return_value = []

    tui._run(screen, timer)

    assert screen.reads == 3
    assert len(captures) == 1
    assert "disk unavailable" in screen.frames[0]
    assert all("disk unavailable" not in frame for frame in screen.frames[1:])
    timer.primary.assert_called_once_with()
    assert screen.now == 0.25


@pytest.mark.parametrize("stage", ["state", "primary"])
def test_unexpected_errors_propagate(loop_environment, stage):
    screen = PacedScreen([ord(" "), ord("q")])
    captures = loop_environment(screen)
    timer = Mock()
    timer.state.return_value = timer_state()
    timer.retained_history.return_value = []
    getattr(timer, stage).side_effect = RuntimeError("unexpected defect")

    with pytest.raises(RuntimeError, match="unexpected defect"):
        tui._run(screen, timer)

    assert not captures
    assert ord("q") in screen.keys


@pytest.mark.parametrize("shape", [(0, 0), (1, 1), (1, 9), (2, 31)])
def test_failure_notice_uses_available_geometry(loop_environment, shape):
    screen = PacedScreen([-1, ord("q")], shape=shape)
    captures = loop_environment(screen)
    timer = Mock()
    attempts = bounded_state(timer, screen, OSError("disk unavailable"))

    tui._run(screen, timer)

    assert screen.reads == len(attempts) == 2
    assert len(captures) == 1
    if shape[1] > 1:
        assert "disk unavailable"[:shape[1] - 1] in screen.frames[-1]


@pytest.mark.parametrize("fails_forever", [False, True])
def test_curses_output_failure_still_reads_quit(
    loop_environment, monkeypatch, fails_forever,
):
    screen = PacedScreen([-1, ord("q")])
    captures = loop_environment(screen)
    timer = Mock()
    timer.state.return_value = timer_state()
    timer.retained_history.return_value = []
    original = screen.addnstr
    writes = []

    def write(*args):
        assert len(writes) < 40, "output failure starved input"
        writes.append(args)
        if fails_forever or len(writes) == 1:
            raise tui.curses.error("output unavailable")
        original(*args)

    monkeypatch.setattr(screen, "addnstr", write)
    tui._run(screen, timer)

    assert screen.reads == 2
    assert screen.now == 0.25
    assert not captures
    if not fails_forever:
        assert "output unavailable" in screen.frames[0]
        assert "output unavailable" not in screen.frames[-1]


def test_draw_and_action_failures_share_capture_budget(loop_environment):
    screen = PacedScreen([ord(" ")] * 24 + [ord("Q")])
    captures = loop_environment(screen)
    timer = Mock()
    attempts = bounded_state(timer, screen, OSError("disk unavailable"))
    timer.primary.side_effect = tui.SharedCoreError("Core unavailable")

    tui._run(screen, timer)

    assert len(attempts) == screen.reads == 25
    assert timer.primary.call_count == 24
    assert screen.now == 6.0
    assert len(captures) == 2


def test_flapping_and_changing_errors_cannot_reset_capture_budget(loop_environment):
    screen = PacedScreen([-1] * 24 + [ord("q")])
    captures = loop_environment(screen)
    timer = Mock()
    timer.state.side_effect = [
        OSError(f"failure {index}") if index % 2 == 0 else timer_state()
        for index in range(25)
    ]
    timer.retained_history.return_value = []

    tui._run(screen, timer)

    assert screen.reads == 25
    assert screen.now == 6.0
    assert [str(error) for error in captures] == ["failure 0", "failure 20"]
    assert all("failure" not in frame for frame in screen.frames[1::2])


def test_fallback_does_not_swallow_unexpected_errors(loop_environment, monkeypatch):
    screen = PacedScreen([ord("q")])
    loop_environment(screen)
    timer = Mock()
    bounded_state(timer, screen, OSError("disk unavailable"))
    monkeypatch.setattr(screen, "refresh", Mock(side_effect=RuntimeError("broken output")))

    with pytest.raises(RuntimeError, match="broken output"):
        tui._run(screen, timer)
