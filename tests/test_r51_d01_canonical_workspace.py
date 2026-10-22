"""R51-D01: canonical duration base follows workspace ownership.

Central install writes a workspace-owned canonical base. Room creation
and room projection initialize the room base from the room projection,
mode switches restore each workspace base, and account reset clears the
central base. A possibly delivered centralized edit stays suppressed.
"""

import json
from contextlib import closing
from unittest.mock import patch

import pytest

from pomodorough.shared_core import SharedCore, apply_projection_v2
from pomodorough.storage import Store, utc_timestamp
from pomodorough.terminal import LocalTimer

BASE_WALL = 1_786_000_000_000
FOCUS_31 = 31 * 60_000
FOCUS_47 = 47 * 60_000
FOCUS_25 = 25 * 60_000
FOCUS_30 = 30 * 60_000
SHORT_BREAK = 5 * 60_000
LONG_BREAK = 15 * 60_000


class ObservedCore(SharedCore):
    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[dict] = []

    def dispatch_json(self, operation: str, input_json: str):
        raw: list[bytes] = []
        original = self._memory.read

        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            raw.append(bytes(result))
            return result

        with patch.object(self._memory, "read", side_effect=capture):
            value = super().dispatch_json(operation, input_json)
        assert len(raw) == 1
        decoded = json.loads(raw[0])
        assert decoded == {"ok": True, "value": value}
        self.envelopes.append({"operation": operation, "value": value})
        return value


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str):
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture
def core():
    return ObservedCore()


@pytest.fixture
def secrets():
    return MemorySecretStore()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "r51d01.sqlite3"


def _response(request, focus_ms, revision, wall):
    specs = {
        "commands": ("acknowledgements", "commandId"),
        "taskOperations": ("taskAcknowledgements", "operationId"),
        "durationOperations": ("durationAcknowledgements", "operationId"),
        "autoStartOperations": ("autoStartAcknowledgements", "operationId"),
        "selectedTaskOperations": ("selectedTaskAcknowledgements", "operationId"),
    }
    response = {
        "revision": revision,
        "canonicalTimer": None,
        "history": [],
        "tasks": [],
        "durationsMs": {
            "focus": focus_ms,
            "short_break": SHORT_BREAK,
            "long_break": LONG_BREAK,
        },
        "autoStartBreaks": False,
        "selectedTaskId": None,
        "serverTime": utc_timestamp(wall),
        "serverHlcWallMs": wall,
        "serverHlcCounter": 0,
    }
    for domain, (key, ident) in specs.items():
        response[key] = [
            {"id": item["id"], "outcome": "applied", "reason": ""}
            for item in request.get(domain, [])
        ]
    return response


def _install(store, focus_ms, revision=1, wall=BASE_WALL):
    request = store.sync_payload()
    assert store.pending_sync() == request
    response = _response(request, focus_ms, revision, wall)
    notices = store.apply_sync(response, request)
    assert notices == []
    assert store.pending_sync() is None
    return request, response


def _focus_of(state):
    return state["settings"]["durationsMs"]["focus"]


def _terminal_planned(store, now_ms):
    return LocalTimer(store).state(now_ms=now_ms)["plannedDurationMs"]


def _room_duration_records(store, room_id):
    rows = store.connection.execute(
        "SELECT record FROM iroh_records WHERE room_id = ? AND domain = 'duration'",
        (room_id,),
    ).fetchall()
    return [json.loads(row["record"]) for row in rows]


def _start_focus(store, now_ms):
    durations = dict(store.load()["settings"]["durationsMs"])
    return store.queue_command("start", None, "focus", durations, now_ms=now_ms)


def test_room_edit_survives_switch_start_reopen(core, secrets, path):
    store = Store(path, shared_core=core, iroh_secret_store=secrets)
    try:
        _install(store, FOCUS_31)
        assert store.get_meta("canonicalDurationsMs")["focus"] == FOCUS_31
        assert core.envelopes, "expected real WASM envelopes"
        room_id = store.create_iroh_room(bytes(range(32)), "R51 room")
        assert store.get_meta("canonicalDurationsMs")["focus"] == FOCUS_31
        store.queue_duration_operation("focus", FOCUS_47, now_ms=BASE_WALL + 1_000)
        assert store.get_meta("canonicalDurationsMs")["focus"] == FOCUS_47
        state = store.load(projection=True)
        assert _focus_of(state) == FOCUS_47
        assert store.projected_state(now_ms=BASE_WALL + 2_000).durations_ms["focus"] == FOCUS_47
        assert _terminal_planned(store, BASE_WALL + 2_000) == FOCUS_47
        records = _room_duration_records(store, room_id)
        assert len(records) == 1
        assert records[0]["operation"]["durationMs"] == FOCUS_47
        assert store.delivery_proof()["durationOperations"] == []
        assert store.load()["pendingDurations"] == []
        store.set_replication_mode("offline")
        assert store.get_meta("canonicalDurationsMs")["focus"] == FOCUS_31
        assert _focus_of(store.load()) == FOCUS_31
        store.set_replication_mode("iroh")
        assert store.get_meta("canonicalDurationsMs")["focus"] == FOCUS_47
        command = _start_focus(store, BASE_WALL + 3_000)
        assert command is not None
        assert command["plannedDurationMs"] == FOCUS_47
        assert _terminal_planned(store, BASE_WALL + 3_000) == FOCUS_47
    finally:
        store.close()
    with closing(Store(path, shared_core=core, iroh_secret_store=secrets)) as reopened:
        assert reopened.get_meta("canonicalDurationsMs")["focus"] == FOCUS_47
        assert _focus_of(reopened.load()) == FOCUS_47
        assert reopened.projected_state(now_ms=BASE_WALL + 4_000).durations_ms["focus"] == FOCUS_47
        assert _terminal_planned(reopened, BASE_WALL + 4_000) == FOCUS_47


def test_signout_clears_base_new_account_start(core, secrets, path):
    store = Store(path, shared_core=core, iroh_secret_store=secrets)
    try:
        _install(store, FOCUS_31)
        store.reset_account_data()
        assert store.get_meta("canonicalDurationsMs") is None
        assert store.pending_sync() is None
        assert store.pending_resolution() is None
        assert all(value == [] for value in store.delivery_proof().values())
        assert store.canonical_head() is None
        state = store.load(projection=True)
        assert _focus_of(state) == FOCUS_25
        assert state["pending"] == []
        assert state["pendingDurations"] == []
        assert store.projected_state(now_ms=BASE_WALL + 1_000).durations_ms["focus"] == FOCUS_25
        assert _terminal_planned(store, BASE_WALL + 1_000) == FOCUS_25
        command = _start_focus(store, BASE_WALL + 2_000)
        assert command is not None
        assert command["plannedDurationMs"] == FOCUS_25
        claim = store.sync_payload()
        assert claim["durationOperations"] == []
        assert store.pending_sync() == claim
    finally:
        store.close()
    with closing(Store(path, shared_core=core, iroh_secret_store=secrets)) as reopened:
        assert reopened.get_meta("canonicalDurationsMs") is None
        assert _focus_of(reopened.load()) == FOCUS_25
        assert reopened.projected_state(now_ms=BASE_WALL + 3_000).durations_ms["focus"] == FOCUS_25
        assert _terminal_planned(reopened, BASE_WALL + 3_000) == FOCUS_25
        assert reopened.pending_sync() == claim


def test_never_synced_control(core, secrets, path):
    store = Store(path, shared_core=core, iroh_secret_store=secrets)
    try:
        assert store.get_meta("canonicalDurationsMs") is None
        assert _focus_of(store.load()) == FOCUS_25
        assert store.projected_state(now_ms=BASE_WALL + 1_000).durations_ms["focus"] == FOCUS_25
        assert _terminal_planned(store, BASE_WALL + 1_000) == FOCUS_25
        command = _start_focus(store, BASE_WALL + 2_000)
        assert command is not None
        assert command["plannedDurationMs"] == FOCUS_25
    finally:
        store.close()
    with closing(Store(path, shared_core=core, iroh_secret_store=secrets)) as reopened:
        assert reopened.get_meta("canonicalDurationsMs") is None
        assert _focus_of(reopened.load()) == FOCUS_25
        assert reopened.projected_state(now_ms=BASE_WALL + 3_000).durations_ms["focus"] == FOCUS_25


def test_possibly_delivered_edit_suppressed(core, secrets, path):
    canon = {"focus": FOCUS_25, "short_break": SHORT_BREAK, "long_break": LONG_BREAK}
    store = Store(path, shared_core=core, iroh_secret_store=secrets)
    try:
        _install(store, FOCUS_25)
        store.queue_duration_operation("focus", FOCUS_30, now_ms=BASE_WALL + 1_000)
        assert _focus_of(store.load()) == FOCUS_30
        assert store.projected_state(now_ms=BASE_WALL + 2_000).durations_ms["focus"] == FOCUS_30
        claim = store.sync_payload()
        assert len(claim["durationOperations"]) == 1
        assert store.delivery_proof()["durationOperations"] == []
    finally:
        store.close()
    with closing(Store(path, shared_core=core, iroh_secret_store=secrets)) as reopened:
        assert reopened.pending_sync() == claim
        assert [item["durationMs"] for item in reopened.load()["pendingDurations"]] == [FOCUS_30]
        projected = reopened.projected_state(now_ms=BASE_WALL + 3_000)
        assert projected.durations_ms["focus"] == FOCUS_25
        oracle = apply_projection_v2(SharedCore(), _oracle_input(canon, BASE_WALL + 3_000))
        assert projected.durations_ms == dict(oracle.durations_ms)
        assert reopened.get_meta("canonicalDurationsMs") == canon


def _oracle_input(canon, now_ms):
    return {
        "base": {
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": dict(canon),
            "autoStartBreaks": False,
            "selectedTaskId": None,
        },
        "pending": {
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        },
        "now": utc_timestamp(now_ms),
    }
