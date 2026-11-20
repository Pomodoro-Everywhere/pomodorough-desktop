"""Start after a finished break works when another device finished it first.

Every device that runs a timer finishes it when it expires. When the other
device's finish reaches the server first, this device's own finish is still
pending and its command id differs from the one the server kept. Pressing
Start on the finished break then raised "Timer changed before restart could
be saved." and the window said "That change was not accepted and nothing was
changed." (Steam Deck, 2026-11-20, Sentry POMODOROUGH-JY).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp
from test_inflight_start_projection import BREAK_MS, DURATIONS, _response, _timer

TIMER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_FINISH_ID = "01a1f7e8-0000-7000-8000-0000000000ff"
RESTART_ERROR = "Timer changed before restart could be saved."


def _running_break(store: Store, start_ms: int) -> dict[str, Any]:
    return {
        "id": TIMER_ID, "phase": "short_break", "status": "running",
        "plannedDurationMs": BREAK_MS, "elapsedAtAnchorMs": 0,
        "anchorAt": utc_timestamp(start_ms), "startedByDeviceId": store.device_id,
        "lastIntent": {"type": "start", "commandId": "01a1f7e8-0000-7000-8000-000000000001",
                       "occurredAt": utc_timestamp(start_ms)},
    }


def _other_device_finish(running: dict[str, Any], end_ms: int) -> tuple[dict[str, Any], list[Any]]:
    completed = dict(running, status="completed", lastIntent={
        "type": "finish", "commandId": OTHER_FINISH_ID, "occurredAt": utc_timestamp(end_ms)})
    history = [{
        "id": TIMER_ID, "timerId": TIMER_ID, "commandId": OTHER_FINISH_ID,
        "phase": "short_break", "status": "completed", "plannedDurationMs": BREAK_MS,
        "elapsedMs": BREAK_MS, "completedAt": utc_timestamp(end_ms), "endedAt": utc_timestamp(end_ms),
    }]
    return completed, history


def _finish_while_sync_in_flight(
    tmp_path: Path, *, other_device_won: bool
) -> tuple[Store, dict[str, Any], int]:
    """Queue this device's finish while a sync is in flight; return the screen timer."""
    store = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    start_ms = int(time.time() * 1000) - BREAK_MS - 3_000
    running = _running_break(store, start_ms)
    request = store.sync_payload()
    store.apply_sync(_response(request, 1, running, [], start_ms), request)
    end_ms = start_ms + BREAK_MS
    in_flight = store.sync_payload()
    store.queue_command("finish", _timer(store, end_ms + 10), "short_break", DURATIONS, None,
                        now_ms=end_ms + 10)
    on_screen = _timer(store, end_ms + 20)
    assert (on_screen["status"], on_screen["lastIntent"]["type"]) == ("completed", "finish")
    timer, history = _other_device_finish(running, end_ms) if other_device_won else (running, [])
    store.apply_sync(_response(in_flight, 2, timer, history, end_ms + 1_000), in_flight)
    return store, on_screen, end_ms


@pytest.mark.parametrize("other_device_won", [True, False], ids=["other-finish-kept", "own-finish-hidden"])
def test_start_on_finished_break_queues_restart(tmp_path, other_device_won):
    store, on_screen, _ = _finish_while_sync_in_flight(tmp_path, other_device_won=other_device_won)

    cleared, started = store.queue_restart(on_screen, "focus", DURATIONS, None)

    assert (cleared["type"], cleared["timerId"]) == ("clear", TIMER_ID)
    assert (started["type"], started["phase"]) == ("start", "focus")
    assert [command["type"] for command in store.load()["pending"]] == ["finish", "clear", "start"]


def test_second_start_press_does_not_queue_another_restart(tmp_path):
    store, on_screen, _ = _finish_while_sync_in_flight(tmp_path, other_device_won=True)
    store.queue_restart(on_screen, "focus", DURATIONS, None)

    with pytest.raises(ValueError, match=RESTART_ERROR):
        store.queue_restart(on_screen, "focus", DURATIONS, None)

    assert [command["type"] for command in store.load()["pending"]] == ["finish", "clear", "start"]


def test_start_on_a_different_finished_timer_is_refused(tmp_path):
    store, on_screen, _ = _finish_while_sync_in_flight(tmp_path, other_device_won=False)
    stranger = dict(on_screen, id="22222222-2222-4222-8222-222222222222")

    with pytest.raises(ValueError, match=RESTART_ERROR):
        store.queue_restart(stranger, "focus", DURATIONS, None)
