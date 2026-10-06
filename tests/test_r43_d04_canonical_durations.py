"""R43-D04: separate canonical duration base from optimistic settings.

Filtering an unsafe queued 30-minute duration must expose canonical 25
after claim, lost response, and reopen. The client persists canonical
durations apart from rendered settings and replays filtered queues
against that base. Differential oracle is production Core
projection.apply.v2 with the canonical 25 base and empty safe queues,
which matches workspace.project.v1 replay for this case. The pinned
bundle predates workspace.project.v1, so the adapter replays via the
older operation until the repin.
"""

from contextlib import closing
from copy import deepcopy

import pytest

from pomodorough.shared_core import SharedCore, apply_projection_v2
from pomodorough.storage import Store, utc_timestamp

CANON = {"focus": 1_500_000, "short_break": 300_000, "long_break": 900_000}
BASE_WALL = 1_786_000_000_000


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    yield instance
    instance.close()


def canon_response(revision=1, wall=BASE_WALL):
    return {
        "acknowledgements": [], "taskAcknowledgements": [],
        "durationAcknowledgements": [], "autoStartAcknowledgements": [],
        "selectedTaskAcknowledgements": [], "revision": revision,
        "canonicalTimer": None, "history": [], "tasks": [],
        "durationsMs": dict(CANON),
        "autoStartBreaks": False, "selectedTaskId": None,
        "serverTime": utc_timestamp(wall),
        "serverHlcWallMs": wall, "serverHlcCounter": 0,
    }


def install_canon(store, revision=1, wall=BASE_WALL):
    request = store.sync_payload()
    store.apply_sync(canon_response(revision, wall), request)


def core_oracle(durations, now_ms):
    return apply_projection_v2(SharedCore(), {
        "base": {"canonicalTimer": None, "history": [], "tasks": [],
                 "durationsMs": dict(durations),
                 "autoStartBreaks": False, "selectedTaskId": None},
        "pending": {"commands": [], "taskOperations": [],
                    "durationOperations": [], "autoStartOperations": [],
                    "selectedTaskOperations": []},
        "now": utc_timestamp(now_ms),
    })


def test_canonical_base_persisted_apart_from_optimistic(store):
    install_canon(store)
    assert store.get_meta("canonicalDurationsMs") == CANON
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL + 1_000)
    assert store.load()["settings"]["durationsMs"]["focus"] == 1_800_000
    assert store.get_meta("canonicalDurationsMs") == CANON


def test_safe_duration_replays_optimistic_30(store):
    install_canon(store)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL + 1_000)
    projected = store.projected_state(now_ms=BASE_WALL + 2_000)
    assert projected.durations_ms["focus"] == 1_800_000


def test_claim_lost_reopen_filters_to_canonical_25(store, tmp_path):
    install_canon(store)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL + 1_000)
    claim = store.sync_payload()
    assert store.delivery_proof()["durationOperations"] == []
    path = tmp_path / "state.sqlite3"
    store.close()
    with closing(Store(path, shared_core=SharedCore())) as reopened:
        assert reopened.pending_sync() == claim
        assert [item["durationMs"] for item in
                reopened.load()["pendingDurations"]] == [1_800_000]
        projected = reopened.projected_state(now_ms=BASE_WALL + 2_000)
        assert projected.durations_ms["focus"] == 1_500_000
        oracle = core_oracle(CANON, BASE_WALL + 2_000)
        assert projected.durations_ms == dict(oracle.durations_ms)
        assert reopened.get_meta("canonicalDurationsMs") == CANON


def test_contaminated_base_still_yields_30_proving_separate_need(store):
    oracle_clean = core_oracle(CANON, BASE_WALL + 2_000)
    assert oracle_clean.durations_ms["focus"] == 1_500_000
    contaminated = dict(CANON, focus=1_800_000)
    oracle_dirty = core_oracle(contaminated, BASE_WALL + 2_000)
    assert oracle_dirty.durations_ms["focus"] == 1_800_000


def test_unrelated_mutation_keeps_duration_filtered(store, tmp_path):
    install_canon(store)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL + 1_000)
    claim = store.sync_payload()
    path = tmp_path / "state.sqlite3"
    store.close()
    with closing(Store(path, shared_core=SharedCore())) as reopened:
        assert reopened.pending_sync() == claim
        reopened.set_auto_start_breaks(True, now_ms=BASE_WALL + 3_000)
        projected = reopened.projected_state(now_ms=BASE_WALL + 4_000)
        assert projected.durations_ms["focus"] == 1_500_000
        oracle = core_oracle(CANON, BASE_WALL + 4_000)
        assert projected.durations_ms["focus"] == oracle.durations_ms["focus"]
        assert deepcopy(reopened.get_meta("canonicalDurationsMs")) == CANON
