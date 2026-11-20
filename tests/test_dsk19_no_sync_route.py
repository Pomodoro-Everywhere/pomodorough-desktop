"""DSK-19: with no account and no sync route the terminal hides pending sync.

The terminal showed "N timer changes pending sync" from the first key press
and the number only grew, although nothing could ever sync. The user decision
is to hide the counters and the row mark, not to reword them.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from pomodorough import tui
from pomodorough.cli import main
from pomodorough.localization import Strings
from pomodorough.storage import Store
from pomodorough.sync_route import sync_route_active
from pomodorough.terminal import LocalTimer

ACCOUNT = {"id": "user-1"}
PENDING_COUNTS = (
    "pendingCommands", "pendingDurationOperations", "pendingAutoStartOperations",
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "state.sqlite3")
    yield opened
    opened.close()


def _queue_every_kind(store: Store) -> LocalTimer:
    """One finished timer, one duration change and one auto-start change."""
    store.queue_duration_operation("focus", 2 * 60_000, now_ms=3)
    store.set_auto_start_breaks(True, now_ms=4)
    timer = LocalTimer(store, strings=Strings("en"))
    timer.issue("start", phase="short-break", minutes=1, now_ms=1_000)
    timer.issue("finish", now_ms=31_000)
    return timer


def _screen(timer: LocalTimer) -> str:
    state = timer.state(now_ms=31_000)
    history = timer.retained_history(now_ms=31_000)
    return "\n".join(tui._screen_lines(state, history, 50, 200, "", timer.strings))


def _cli(store: Store, *arguments: str) -> str:
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(arguments, store=store, stdout=stdout, stderr=stderr) == 0
    assert stderr.getvalue() == ""
    return stdout.getvalue()


@pytest.mark.parametrize(
    ("mode", "signed_in", "active"),
    [
        ("offline", False, False),
        ("offline", True, False),
        ("centralized", False, False),
        ("centralized", True, True),
        ("iroh", False, True),
        ("iroh", True, True),
    ],
)
def test_route_rule(mode: str, signed_in: bool, active: bool) -> None:
    assert sync_route_active(mode, signed_in=signed_in) is active


@pytest.mark.parametrize("mode", ["centralized", "offline"])
def test_no_account_hides_pending_sync_on_the_screen(store: Store, mode: str) -> None:
    store.set_replication_mode(mode)
    timer = _queue_every_kind(store)

    state = timer.state(now_ms=31_000)
    screen = _screen(timer)

    assert not timer.sync_route_active
    assert [state[count] for count in PENDING_COUNTS] == [0, 0, 0]
    assert "pending sync" not in screen
    assert "Short break" in screen  # the arrival row itself stays
    # The queue is hidden, not dropped: it syncs once a route exists.
    stored = store.load()
    assert stored["pending"] and stored["pendingDurations"]
    assert stored["pendingAutoStarts"]


def test_no_account_hides_pending_sync_in_the_cli(store: Store) -> None:
    _queue_every_kind(store)

    status = json.loads(_cli(store, "status", "--json"))
    arrivals = json.loads(_cli(store, "history", "--json"))

    assert "pending sync" not in _cli(store, "status")
    assert "pending sync" not in _cli(store, "history")
    assert [status[count] for count in PENDING_COUNTS] == [0, 0, 0]
    assert [item["pending"] for item in arrivals] == [False]


def test_account_shows_pending_sync(store: Store) -> None:
    store.set_user(ACCOUNT)
    timer = _queue_every_kind(store)

    state = timer.state(now_ms=31_000)
    screen = _screen(timer)

    assert timer.sync_route_active
    assert all(state[count] > 0 for count in PENDING_COUNTS)
    for key in ("terminal.pending_commands", "terminal.pending_durations",
                "terminal.pending_auto_start"):
        assert "pending sync" in screen
        assert any(
            timer.strings.plural(key, count) in screen for count in range(1, 6)
        ), key
    assert timer.strings.text("tui.pending_mark") in screen
    assert [item["pending"] for item in timer.retained_history(now_ms=31_000)] == [True]


def test_account_removed_hides_pending_sync_again(store: Store) -> None:
    store.set_user(ACCOUNT)
    timer = _queue_every_kind(store)
    assert "pending sync" in _screen(timer)

    store.set_user(None)

    assert "pending sync" not in _screen(timer)
    assert not timer.sync_route_active


def test_account_waiting_for_history_resolution_counts_as_an_account(
    store: Store,
) -> None:
    timer = _queue_every_kind(store)
    store.prepare_resolution(ACCOUNT, 4, "merge")

    state = timer.state(now_ms=31_000)

    assert timer.sync_route_active
    assert state["historyResolutionPending"]
    assert state["pendingCommands"] > 0


def test_history_resolution_line_does_not_depend_on_the_route() -> None:
    state = {count: 0 for count in PENDING_COUNTS} | {"historyResolutionPending": True}
    assert tui._pending_lines(state, Strings("en")) == [
        Strings("en").text("terminal.history_resolution_pending")
    ]
