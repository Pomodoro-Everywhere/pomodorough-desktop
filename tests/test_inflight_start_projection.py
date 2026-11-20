"""A Start pressed while a sync is in flight stays on screen.

Projection hides a never-sent operation that is not newer than the canonical
head. Every sync response carries serverHlc*, the server's HLC at render time,
which includes the server's own clock and so is ahead of any operation the
client queued while that request was travelling. A server that also sends
canonicalHlcWallMs/canonicalHlcCounter names the highest HLC it ordered into
canonical state; the client installs that pair as the head (receiver rule,
scratch/r55/fix/R55-C14/CONTRACT.md). An older server sends only serverHlc*:
when the revision did not move, the stored head still covers the snapshot.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp

DURATIONS = {"focus": 25 * 60_000, "short_break": 5 * 60_000, "long_break": 15 * 60_000}
BREAK_MS = 5 * 60_000
MAX_SAFE_INTEGER = 2**53 - 1
START_OFFSET_MS = 2_500
SERVER_OFFSET_MS = 3_000


class RecordingCore(SharedCore):
    """Real Core that records the responses passed to reconciliation."""

    def __init__(self) -> None:
        super().__init__()
        self.reconciled_responses: list[dict[str, Any]] = []

    def dispatch(self, operation: str, input_value: object) -> object:
        if operation == "reconcile.rebase.v2" and isinstance(input_value, dict):
            self.reconciled_responses.append(input_value["response"])
        return super().dispatch(operation, input_value)


def _response(
    request: dict[str, Any],
    revision: int,
    timer: Any,
    history: list[Any],
    at_ms: int,
    canonical: tuple[int, int] | None = None,
) -> dict[str, Any]:
    response = {
        "acknowledgements": [
            {"commandId": command["id"], "outcome": "applied", "reason": ""}
            for command in request.get("commands", [])
        ],
        "taskAcknowledgements": [],
        "durationAcknowledgements": [],
        "autoStartAcknowledgements": [],
        "selectedTaskAcknowledgements": [],
        "revision": revision,
        "canonicalTimer": timer,
        "history": history,
        "tasks": [],
        "durationsMs": DURATIONS,
        "autoStartBreaks": False,
        "selectedTaskId": None,
        "serverTime": utc_timestamp(at_ms),
        "serverHlcWallMs": at_ms,
        "serverHlcCounter": 0,
    }
    if canonical is not None:
        response["canonicalHlcWallMs"], response["canonicalHlcCounter"] = canonical
    return response


def _finished_break(
    tmp_path: Path, core: SharedCore | None = None
) -> tuple[Store, int, dict[str, Any], list[Any]]:
    """Return a store whose finished break the server acknowledged at revision 2."""
    store = Store(tmp_path / "state.sqlite3", shared_core=core or SharedCore())
    start_ms = int(time.time() * 1000) - BREAK_MS - 3_000
    running = {
        "id": "11111111-1111-4111-8111-111111111111",
        "phase": "short_break",
        "status": "running",
        "plannedDurationMs": BREAK_MS,
        "elapsedAtAnchorMs": 0,
        "anchorAt": utc_timestamp(start_ms),
        "startedByDeviceId": store.device_id,
        "lastIntent": {
            "type": "start",
            "commandId": "01a1f7e8-0000-7000-8000-000000000001",
            "occurredAt": utc_timestamp(start_ms),
        },
    }
    request = store.sync_payload()
    store.apply_sync(_response(request, 1, running, [], start_ms), request)
    end_ms = start_ms + BREAK_MS
    timer = _timer(store, end_ms + 10)
    store.queue_command("finish", timer, "short_break", DURATIONS, None, now_ms=end_ms + 10)
    request = store.sync_payload()
    finish_id = request["commands"][0]["id"]
    completed = dict(timer, status="completed", lastIntent={
        "type": "finish", "commandId": finish_id, "occurredAt": utc_timestamp(end_ms)})
    history = [{
        "id": timer["id"], "timerId": timer["id"], "commandId": finish_id,
        "phase": "short_break", "status": "completed", "plannedDurationMs": BREAK_MS,
        "elapsedMs": BREAK_MS, "completedAt": utc_timestamp(end_ms), "endedAt": utc_timestamp(end_ms),
    }]
    store.apply_sync(_response(request, 2, completed, history, end_ms + 1_500), request)
    return store, end_ms, completed, history


def _timer(store: Store, now_ms: int) -> dict[str, Any] | None:
    state = store.load(projection=True)
    return store.projected_state(now_ms=now_ms, state=state).canonical_timer


def _queue_start_during_sync(
    tmp_path: Path, core: SharedCore | None = None
) -> tuple[Store, int, dict[str, Any], list[Any], dict[str, Any]]:
    store, end_ms, completed, history = _finished_break(tmp_path, core)
    in_flight = store.sync_payload()
    assert in_flight["commands"] == []
    store.queue_command(
        "start", None, "focus", DURATIONS, None, now_ms=end_ms + START_OFFSET_MS
    )
    start = store.load()["pending"][0]
    assert (start["hlcWallMs"], start["hlcCounter"]) == (end_ms + START_OFFSET_MS, 0)
    assert _timer(store, end_ms + 2_600)["phase"] == "focus"
    return store, end_ms, completed, history, in_flight


def _start_during_sync(
    tmp_path: Path,
    response_revision: int,
    canonical_offset_ms: int | None = None,
    core: SharedCore | None = None,
) -> tuple[Store, int]:
    store, end_ms, completed, history, in_flight = _queue_start_during_sync(tmp_path, core)
    canonical = None if canonical_offset_ms is None else (end_ms + canonical_offset_ms, 0)
    response = _response(
        in_flight, response_revision, completed, history, end_ms + SERVER_OFFSET_MS, canonical
    )
    store.apply_sync(response, in_flight)
    return store, end_ms


def _assert_focus_running(store: Store, end_ms: int) -> None:
    timer = _timer(store, end_ms + 3_100)
    assert timer is not None
    assert (timer["phase"], timer["status"]) == ("focus", "running")


def test_start_stays_projected_when_canonical_head_is_older_than_start(tmp_path):
    # The user's case: serverHlc* is newer than the Start, the canonical pair
    # is older. Another device moved the revision, so only the pair helps.
    store, end_ms = _start_during_sync(tmp_path, response_revision=3, canonical_offset_ms=2_000)

    _assert_focus_running(store, end_ms)
    assert store.canonical_head() == (end_ms + 2_000, 0)


def test_start_stays_hidden_when_canonical_head_is_newer_than_start(tmp_path):
    # Another device's later operation may sort after the local Start, so the
    # fail-closed rule keeps hiding it until the server orders both.
    store, end_ms = _start_during_sync(tmp_path, response_revision=3, canonical_offset_ms=2_800)

    assert store.canonical_head() == (end_ms + 2_800, 0)
    assert _timer(store, end_ms + 3_100) is None


def test_canonical_pair_replaces_stored_head_at_unchanged_revision(tmp_path):
    store, end_ms = _start_during_sync(tmp_path, response_revision=2, canonical_offset_ms=1_000)

    _assert_focus_running(store, end_ms)
    assert store.canonical_head() == (end_ms + 1_000, 0)


def test_canonical_pair_is_installed_as_sent_at_unchanged_revision(tmp_path):
    # The pair is authoritative: the stored older-server bound is not consulted.
    store, end_ms = _start_during_sync(tmp_path, response_revision=2, canonical_offset_ms=1_800)

    _assert_focus_running(store, end_ms)
    assert store.canonical_head() == (end_ms + 1_800, 0)


def test_reconciliation_passes_canonical_pair_to_core(tmp_path):
    core = RecordingCore()
    _store, end_ms = _start_during_sync(
        tmp_path, response_revision=3, canonical_offset_ms=2_000, core=core
    )

    response = core.reconciled_responses[-1]
    assert (response["canonicalHlcWallMs"], response["canonicalHlcCounter"]) == (end_ms + 2_000, 0)
    assert response["serverHlcWallMs"] == end_ms + SERVER_OFFSET_MS


def test_reconciliation_omits_canonical_pair_the_server_did_not_send(tmp_path):
    core = RecordingCore()
    _start_during_sync(tmp_path, response_revision=3, core=core)

    response = core.reconciled_responses[-1]
    assert "canonicalHlcWallMs" not in response
    assert "canonicalHlcCounter" not in response


def test_start_stays_projected_when_older_server_returns_same_revision(tmp_path):
    store, end_ms = _start_during_sync(tmp_path, response_revision=2)

    _assert_focus_running(store, end_ms)
    assert store.canonical_head() == (end_ms + 1_500, 0)


def test_head_follows_older_server_clock_when_revision_moved(tmp_path):
    store, end_ms = _start_during_sync(tmp_path, response_revision=3)

    assert store.canonical_head() == (end_ms + SERVER_OFFSET_MS, 0)
    assert _timer(store, end_ms + 3_100) is None


def _only_wall(response: dict[str, Any]) -> None:
    response.pop("canonicalHlcCounter")


def _only_counter(response: dict[str, Any]) -> None:
    response.pop("canonicalHlcWallMs")


def _wall_above_server(response: dict[str, Any]) -> None:
    response["canonicalHlcWallMs"] = response["serverHlcWallMs"] + 1


def _counter_above_server(response: dict[str, Any]) -> None:
    response["canonicalHlcWallMs"] = response["serverHlcWallMs"]
    response["canonicalHlcCounter"] = response["serverHlcCounter"] + 1


def _null_pair(response: dict[str, Any]) -> None:
    response["canonicalHlcWallMs"] = None
    response["canonicalHlcCounter"] = None


def _quoted_pair(response: dict[str, Any]) -> None:
    # A JSON string that holds a number is not a JSON integer.
    response["canonicalHlcWallMs"] = str(response["canonicalHlcWallMs"])
    response["canonicalHlcCounter"] = str(response["canonicalHlcCounter"])


def _setter(key: str, value: Any) -> Any:
    def mutate(response: dict[str, Any]) -> None:
        response[key] = value
    return mutate


MALFORMED_PAIRS = {
    "only wall": _only_wall,
    "only counter": _only_counter,
    "wall above server": _wall_above_server,
    "counter above server": _counter_above_server,
    "negative wall": _setter("canonicalHlcWallMs", -1),
    "negative counter": _setter("canonicalHlcCounter", -1),
    "float wall": _setter("canonicalHlcWallMs", 1_000.0),
    "string counter": _setter("canonicalHlcCounter", "0"),
    "boolean counter": _setter("canonicalHlcCounter", False),
    "null wall": _setter("canonicalHlcWallMs", None),
    "null pair": _null_pair,
    "quoted pair": _quoted_pair,
    # A 2^53 wall is refused as above the server pair: an accepted server wall
    # never exceeds year 9999 plus the 5-minute skew, far below 2^53.
    "wall 2^53 above server": _setter("canonicalHlcWallMs", MAX_SAFE_INTEGER + 1),
    "unsafe counter": _setter("canonicalHlcCounter", MAX_SAFE_INTEGER + 1),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_PAIRS))
def test_malformed_canonical_pair_fails_closed(tmp_path, case):
    store, end_ms, completed, history, in_flight = _queue_start_during_sync(tmp_path)
    response = _response(
        in_flight, 3, completed, history, end_ms + SERVER_OFFSET_MS, (end_ms + 2_000, 0)
    )
    MALFORMED_PAIRS[case](response)
    before = (store.load(), store.canonical_head(), store.pending_sync())

    with pytest.raises(ValueError, match="invalid logical clock"):
        store.apply_sync(response, in_flight)

    assert (store.load(), store.canonical_head(), store.pending_sync()) == before


def test_canonical_pair_equal_to_server_pair_is_accepted(tmp_path):
    store, end_ms = _start_during_sync(
        tmp_path, response_revision=3, canonical_offset_ms=SERVER_OFFSET_MS
    )

    assert store.canonical_head() == (end_ms + SERVER_OFFSET_MS, 0)


def test_canonical_counter_at_max_safe_integer_is_accepted(tmp_path):
    # 2^53 - 1 is the largest JavaScript-safe integer, so it is still a valid
    # wire value; 2^53 is refused by the table above. The wall cannot reach
    # 2^53 - 1 here: the pair may not exceed serverHlc*, whose wall stays
    # within the clock skew of an ISO serverTime.
    store, end_ms, completed, history, in_flight = _queue_start_during_sync(tmp_path)
    pair = (end_ms + 2_000, MAX_SAFE_INTEGER)
    response = _response(in_flight, 3, completed, history, end_ms + SERVER_OFFSET_MS, pair)

    store.apply_sync(response, in_flight)

    assert store.canonical_head() == pair
    _assert_focus_running(store, end_ms)


def test_resolution_installs_canonical_pair(tmp_path):
    store = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    user = {"id": "user-1"}
    now_ms = int(time.time() * 1000)
    request = store.prepare_resolution(user, 0, "keep_remote")
    response = _response(request, 1, None, [], now_ms, (now_ms - 5_000, 3))

    store.apply_resolution(response, user, request_id=request["requestId"])

    assert store.canonical_head() == (now_ms - 5_000, 3)


def test_bootstrap_plan_rejects_lone_canonical_field(tmp_path):
    store = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    now_ms = int(time.time() * 1000)
    response = _response({}, 1, None, [], now_ms)
    response["canonicalHlcWallMs"] = now_ms - 5_000

    with pytest.raises(ValueError, match="invalid logical clock"):
        store.bootstrap_resolution_plan(response)
