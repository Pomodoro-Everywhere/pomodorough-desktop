import sqlite3
from contextlib import closing
from copy import deepcopy
from itertools import permutations

import pytest

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    yield instance
    instance.close()


def completion(index, phase="focus"):
    stamp = utc_timestamp(index * 1_000)
    return {
        "id": f"timer-{index}", "timerId": f"timer-{index}",
        "commandId": f"finish-{index}", "phase": phase,
        "status": "completed", "plannedDurationMs": 60_000,
        "completedAt": stamp, "endedAt": stamp,
    }


def response(history, revision=1):
    return {
        "acknowledgements": [], "taskAcknowledgements": [],
        "durationAcknowledgements": [], "autoStartAcknowledgements": [],
        "selectedTaskAcknowledgements": [], "revision": revision,
        "canonicalTimer": None, "history": deepcopy(history), "tasks": [],
        "durationsMs": {"focus": 1_500_000, "short_break": 300_000,
                        "long_break": 900_000},
        "autoStartBreaks": False, "selectedTaskId": None,
        "serverTime": utc_timestamp(100_000),
        "serverHlcWallMs": 100_000, "serverHlcCounter": 0,
    }


def sync(store, history, revision=1):
    request = store.sync_payload()
    store.apply_sync(response(history, revision), request)


def selected(store):
    return store.load()["settings"]["selectedPhase"]


@pytest.mark.parametrize("phase,count,derived", [
    ("focus", 1, "short_break"), ("focus", 4, "long_break"),
    ("short_break", 1, "focus"), ("long_break", 1, "focus"),
])
def test_unchanged_completion_preserves_explicit_phase_after_restart(
    store, tmp_path, phase, count, derived,
):
    history = [completion(index, phase) for index in range(1, count + 1)]
    store.set_selected_phase(phase)
    sync(store, history)
    assert selected(store) == derived
    sync(store, history)
    assert selected(store) == derived
    store.set_selected_phase(phase)
    sync(store, history)
    assert selected(store) == phase
    store.close()
    with closing(Store(tmp_path / "state.sqlite3", shared_core=SharedCore())) as reopened:
        sync(reopened, history)
        sync(reopened, list(reversed(history)), revision=2)
        assert selected(reopened) == phase


def test_historical_merge_and_reordering_do_not_replay_known_completion(store):
    history = [completion(3), completion(4)]
    sync(store, history)
    store.set_selected_phase("focus")
    sync(store, [completion(1), *reversed(history), completion(2)], revision=2)
    assert selected(store) == "focus"
    sync(store, [*history, completion(5)], revision=3)
    assert selected(store) == "short_break"


def test_equal_time_reordering_does_not_replay_known_completion(store):
    history = [completion(1), completion(2)]
    history[1]["completedAt"] = history[0]["completedAt"]
    history[1]["endedAt"] = history[0]["endedAt"]
    sync(store, history)
    store.set_selected_phase("focus")
    sync(store, list(reversed(history)), revision=2)
    assert selected(store) == "focus"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("phase,count,derived", [
    ("focus", 2, "short_break"), ("focus", 4, "long_break"),
    ("short_break", 2, "focus"), ("long_break", 2, "focus"),
])
def test_unseen_completion_tied_with_known_advances_once(
    store, tmp_path, reverse, phase, count, derived,
):
    known = [completion(index, phase) for index in range(1, count)]
    fresh = completion(count, phase)
    fresh.update(completedAt=known[-1]["completedAt"], endedAt=known[-1]["endedAt"])
    store.set_selected_phase(phase)
    sync(store, known)
    store.set_selected_phase(phase)
    history = [*known, fresh]
    if reverse:
        history.reverse()
    sync(store, history, revision=2)
    assert selected(store) == derived
    sync(store, list(reversed(history)), revision=2)
    assert selected(store) == derived
    store.set_selected_phase(phase)
    sync(store, history, revision=2)
    assert selected(store) == phase
    store.close()
    with closing(Store(tmp_path / "state.sqlite3", shared_core=SharedCore())) as reopened:
        assert selected(reopened) == phase
        sync(reopened, list(reversed(history)), revision=2)
        assert selected(reopened) == phase


@pytest.mark.parametrize("reverse", [False, True])
def test_older_unseen_completion_is_not_selected_after_known_latest(store, reverse):
    known = completion(3)
    sync(store, [known])
    store.set_selected_phase("focus")
    history = [known, completion(1), completion(2)]
    if reverse:
        history.reverse()
    sync(store, history, revision=2)
    assert selected(store) == "focus"


@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("phase,other_phase,derived", [
    ("focus", "short_break", "short_break"),
    ("short_break", "focus", "focus"),
])
def test_multiple_unseen_ties_follow_core_timer_id_order(
    store, order, phase, other_phase, derived,
):
    history = [completion(1), completion(2, phase), completion(3, other_phase)]
    for item in history[1:]:
        item.update(completedAt=history[0]["completedAt"], endedAt=history[0]["endedAt"])
    sync(store, history[:1])
    store.set_selected_phase(phase)
    ordered = [history[index] for index in order]
    projection = SharedCore().dispatch("timer.reduce.v1", {
        "canonicalTimer": None, "history": ordered,
        "commands": [], "now": utc_timestamp(100_000),
    })
    assert [item["timerId"] for item in projection["history"]] == [
        "timer-1", "timer-2", "timer-3",
    ]
    sync(store, ordered, revision=2)
    assert selected(store) == derived


def test_history_metadata_change_does_not_replay_completion(store):
    history = [completion(1)]
    sync(store, history)
    store.set_selected_phase("focus")
    history[0].update(completedAt=utc_timestamp(2_000), endedAt=utc_timestamp(2_000))
    sync(store, history, revision=2)
    assert selected(store) == "focus"


def test_completion_accepted_with_different_choice_is_not_deferred(store):
    history = [completion(1)]
    store.set_selected_phase("long_break")
    sync(store, history)
    assert selected(store) == "long_break"
    store.set_selected_phase("focus")
    sync(store, history)
    assert selected(store) == "focus"


def test_failed_install_does_not_consume_completion(store):
    history = [completion(1)]
    request = store.sync_payload()
    previous = store.get_meta("snapshot")
    store.connection.execute("""
        CREATE TEMP TRIGGER fail_phase_write BEFORE INSERT ON meta
        WHEN NEW.key = 'settings'
          AND json_extract(NEW.value, '$.selectedPhase') = 'short_break'
        BEGIN SELECT RAISE(ABORT, 'injected phase persistence failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected phase persistence failure"):
        store.apply_sync(response(history), request)
    store.connection.execute("DROP TRIGGER fail_phase_write")
    assert store.get_meta("snapshot") == previous
    assert store.pending_sync() == request
    assert selected(store) == "focus"
    store.apply_sync(response(history), request)
    assert selected(store) == "short_break"


@pytest.mark.parametrize("strategy", ["keep_remote", "merge", "replace_remote"])
def test_resolution_installs_completion_once_then_sync_preserves_choice(
    store, tmp_path, strategy,
):
    history = [completion(1)]
    user = {"id": "remote-user"}
    request = store.prepare_resolution(user, 0, strategy)
    store.apply_resolution(response(history), user, request["requestId"])
    assert selected(store) == "short_break"
    store.set_selected_phase("focus")
    store.close()
    with closing(Store(tmp_path / "state.sqlite3", shared_core=SharedCore())) as reopened:
        sync(reopened, history)
        assert selected(reopened) == "focus"


def test_explicit_resolution_keeps_existing_install_semantics(store):
    history = [completion(1)]
    sync(store, history)
    store.set_selected_phase("focus")
    user = {"id": "remote-user"}
    request = store.prepare_resolution(user, 1, "keep_remote")
    store.apply_resolution(response(history, 2), user, request["requestId"])
    assert selected(store) == "short_break"
