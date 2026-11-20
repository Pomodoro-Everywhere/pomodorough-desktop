"""History resolution, the bootstrap plan and restart follow the receiver rule.

A response that carries canonicalHlcWallMs/canonicalHlcCounter names the
highest HLC the server ordered into canonical state, and the client installs
that pair as the canonical head; a response without the pair installs
serverHlc* (receiver rule, scratch/r55/fix/R55-C14/CONTRACT.md).
test_inflight_start_projection covers the sync path. These tests cover the
other entry points: a keep-remote resolution installs the response head even
when the store already holds one, a malformed pair is refused by resolution
and by the bootstrap plan without changing the store, and an installed pair
survives a restart.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import pytest

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from test_inflight_start_projection import (
    DURATIONS,
    _assert_focus_running,
    _null_pair,
    _only_counter,
    _only_wall,
    _quoted_pair,
    _response,
    _start_during_sync,
    _wall_above_server,
)

USER = {"id": "user-1"}
STORED_HEAD_AGE_MS = 10_000
STORED_HEAD_COUNTER = 7

MALFORMED_RESPONSE_PAIRS: dict[str, Callable[[dict[str, Any]], None]] = {
    "only wall": _only_wall,
    "only counter": _only_counter,
    "wall above server": _wall_above_server,
    "null pair": _null_pair,
    "quoted pair": _quoted_pair,
}


def _store_with_head(tmp_path: Path) -> tuple[Store, int]:
    """Return a store whose first sync installed a canonical head."""
    store = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    now_ms = int(time.time() * 1000)
    stored_head = (now_ms - STORED_HEAD_AGE_MS, STORED_HEAD_COUNTER)
    request = store.sync_payload()
    store.apply_sync(
        _response(request, 1, None, [], now_ms - 1_000, stored_head), request
    )
    assert store.canonical_head() == stored_head
    return store, now_ms


def _store_state(store: Store) -> tuple[Any, ...]:
    return (
        store.load(),
        store.canonical_head(),
        store.pending_sync(),
        store.pending_resolution(USER["id"]),
    )


@pytest.mark.parametrize("pair_age_ms", [20_000, 5_000], ids=["older", "newer"])
def test_keep_remote_resolution_installs_response_pair_over_stored_head(
    tmp_path, pair_age_ms
):
    # The pair is installed as sent, whether it is older or newer than the
    # head an earlier sync stored.
    store, now_ms = _store_with_head(tmp_path)
    request = store.prepare_resolution(USER, 1, "keep_remote")
    pair = (now_ms - pair_age_ms, 1)
    response = _response(request, 2, None, [], now_ms, pair)

    store.apply_resolution(response, USER, request_id=request["requestId"])

    assert store.canonical_head() == pair
    assert store.pending_resolution(USER["id"]) is None


def test_keep_remote_resolution_without_pair_installs_server_clock_over_stored_head(
    tmp_path,
):
    store, now_ms = _store_with_head(tmp_path)
    request = store.prepare_resolution(USER, 1, "keep_remote")
    response = _response(request, 2, None, [], now_ms)

    store.apply_resolution(response, USER, request_id=request["requestId"])

    assert store.canonical_head() == (now_ms, 0)


@pytest.mark.parametrize("case", sorted(MALFORMED_RESPONSE_PAIRS))
def test_resolution_refuses_malformed_pair_and_keeps_store(tmp_path, case):
    store, now_ms = _store_with_head(tmp_path)
    request = store.prepare_resolution(USER, 1, "keep_remote")
    response = _response(request, 2, None, [], now_ms, (now_ms - 5_000, 3))
    MALFORMED_RESPONSE_PAIRS[case](response)
    before = _store_state(store)

    with pytest.raises(ValueError, match="invalid logical clock"):
        store.apply_resolution(response, USER, request_id=request["requestId"])

    assert _store_state(store) == before
    assert before[3] is not None, "the resolution stays pending for a retry"


@pytest.mark.parametrize("case", sorted(MALFORMED_RESPONSE_PAIRS))
def test_bootstrap_plan_refuses_malformed_pair_and_keeps_store(tmp_path, case):
    store, now_ms = _store_with_head(tmp_path)
    store.queue_command("start", None, "focus", DURATIONS, None, now_ms=now_ms)
    response = _response({}, 2, None, [], now_ms, (now_ms - 5_000, 3))
    MALFORMED_RESPONSE_PAIRS[case](response)
    before = _store_state(store)
    assert before[0]["pending"], "a queued Start is part of the kept state"

    with pytest.raises(ValueError, match="invalid logical clock"):
        store.bootstrap_resolution_plan(response)

    assert _store_state(store) == before


def test_bootstrap_plan_accepts_valid_pair_without_installing_it(tmp_path):
    store, now_ms = _store_with_head(tmp_path)
    response = _response({}, 2, None, [], now_ms, (now_ms - 5_000, 3))
    before = _store_state(store)

    plan = store.bootstrap_resolution_plan(response)

    assert plan["expectedRevision"] == 2
    assert _store_state(store) == before


def test_installed_pair_survives_restart(tmp_path):
    store, end_ms = _start_during_sync(
        tmp_path, response_revision=3, canonical_offset_ms=2_000
    )
    store.close()

    reopened = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())

    assert reopened.canonical_head() == (end_ms + 2_000, 0)
    _assert_focus_running(reopened, end_ms)
