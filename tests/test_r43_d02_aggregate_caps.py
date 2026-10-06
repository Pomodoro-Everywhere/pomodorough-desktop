"""R43-D02: aggregate sync caps through Core sync.batchPlan.v1.

Normal sync batches go through the Core planner with per-domain 256
and aggregate 512 limits instead of slicing each domain independently.
Bootstrap validation enforces per-domain 4096 and aggregate 8192.
Saved claims replay exactly; oversized saved claims fail closed with
an explicit confirmed discard for safe recovery.
"""

import json
from contextlib import closing
from copy import deepcopy

import pytest

from pomodorough.core import task_from_title
from pomodorough.shared_core import SharedCore, SharedCoreOperationError
from pomodorough.storage import Store, utc_timestamp

BASE_WALL_MS = 1_786_000_000_000
DOMAINS = (
    "commands",
    "taskOperations",
    "durationOperations",
    "autoStartOperations",
    "selectedTaskOperations",
)


def _bundle_supports_batch_plan():
    try:
        SharedCore().dispatch(
            "sync.batchPlan.v1",
            {
                "kind": "new",
                "mode": "sync",
                "limits": {"perDomain": 256, "total": 512},
                "nextDomain": "commands",
                "queues": {domain: [] for domain in DOMAINS},
                "timerDependencies": [],
            },
        )
    except SharedCoreOperationError as error:
        return "unsupported" not in str(error)
    return True


needs_batch_plan = pytest.mark.skipif(
    not _bundle_supports_batch_plan(),
    reason="pinned Core predates sync.batchPlan.v1; needs the Core repin",
)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "state.sqlite3", shared_core=SharedCore())
    yield instance
    instance.close()


def payload_total(payload):
    return sum(len(payload[domain]) for domain in DOMAINS)


def seed_commands(store, count, first_index=0):
    rows = []
    for offset in range(count):
        index = first_index + offset
        wall = BASE_WALL_MS + index
        payload = {
            "id": f"r43-cmd-{index:05d}",
            "deviceSequence": offset + 1,
            "timerId": f"r43-timer-{index:05d}",
            "type": "start",
            "phase": "focus",
            "plannedDurationMs": 1_500_000,
            "occurredAt": utc_timestamp(wall),
            "hlcWallMs": wall,
            "hlcCounter": 0,
            "observedElapsedMs": 0,
        }
        rows.append((
            payload["id"],
            payload["deviceSequence"],
            json.dumps(payload, separators=(",", ":")),
        ))
    store.connection.executemany(
        "INSERT INTO pending_commands(id, device_sequence, payload,"
        " depends_on_command_id) VALUES (?, ?, ?, NULL)",
        rows,
    )
    _cover_seeded(store, "commands", [row[0] for row in rows],
                   count, BASE_WALL_MS + first_index + count - 1)


def seed_tasks(store, count, first_index=0):
    rows = []
    for offset in range(count):
        index = first_index + offset
        wall = BASE_WALL_MS + 50_000 + index
        identity = task_from_title(f"R43 aggregate task {index}")
        payload = {
            "id": f"r43-top-{index:05d}",
            "taskId": identity["id"],
            "type": "upsert",
            "title": identity["title"],
            "occurredAt": utc_timestamp(wall),
            "hlcWallMs": wall,
            "hlcCounter": 0,
        }
        rows.append((
            payload["id"],
            json.dumps(payload, separators=(",", ":")),
        ))
    store.connection.executemany(
        "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
        rows,
    )
    _cover_seeded(store, "taskOperations", [row[0] for row in rows],
                   0, BASE_WALL_MS + 50_000 + first_index + count - 1)


def _cover_seeded(store, domain, ids, sequence, wall):
    store.connection.commit()
    proof = store.delivery_proof()
    proof[domain].extend(item for item in ids if item not in proof[domain])
    store.set_meta("deliveryProof", proof)
    if sequence:
        store.set_meta("deviceSequence", sequence)
    clock = store.get_meta("hlc", {"wallMs": 0, "counter": 0})
    if wall > clock.get("wallMs", 0):
        store.set_meta("hlc", {"wallMs": wall, "counter": 0})


def ack_response(request, revision=1):
    wall = max(
        [item["hlcWallMs"] for domain in DOMAINS for item in request[domain]]
        or [1_000]
    )
    auto = request["autoStartOperations"]
    return {
        "acknowledgements": [
            {"commandId": item["id"], "outcome": "applied", "reason": ""}
            for item in request["commands"]
        ],
        "taskAcknowledgements": [
            {"operationId": item["id"], "outcome": "applied", "reason": ""}
            for item in request["taskOperations"]
        ],
        "durationAcknowledgements": [
            {"operationId": item["id"], "outcome": "applied", "reason": ""}
            for item in request["durationOperations"]
        ],
        "autoStartAcknowledgements": [
            {"operationId": item["id"], "outcome": "applied", "reason": ""}
            for item in auto
        ],
        "selectedTaskAcknowledgements": [
            {"operationId": item["id"], "outcome": "applied", "reason": ""}
            for item in request["selectedTaskOperations"]
        ],
        "revision": revision,
        "canonicalTimer": None,
        "history": [],
        "tasks": [],
        "durationsMs": {
            "focus": 1_500_000, "short_break": 300_000,
            "long_break": 900_000,
        },
        "autoStartBreaks": auto[-1]["enabled"] if auto else False,
        "selectedTaskId": None,
        "serverTime": utc_timestamp(wall),
        "serverHlcWallMs": wall,
        "serverHlcCounter": 0,
    }


@needs_batch_plan
def test_normal_batch_accepts_exactly_512(store):
    seed_commands(store, 256)
    seed_tasks(store, 256)
    payload = store.sync_payload()
    assert payload_total(payload) == 512
    assert len(payload["commands"]) == 256
    assert len(payload["taskOperations"]) == 256
    assert [item["id"] for item in payload["commands"]] == [
        f"r43-cmd-{index:05d}" for index in range(256)
    ]


@needs_batch_plan
def test_normal_batch_caps_513_at_aggregate_512(store):
    seed_commands(store, 256)
    seed_tasks(store, 256)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL_MS)
    payload = store.sync_payload()
    assert payload_total(payload) == 512
    for domain in DOMAINS:
        assert len(payload[domain]) <= 256


@needs_batch_plan
def test_oversized_claim_reopens_replays_and_drains(store, tmp_path):
    seed_commands(store, 256)
    seed_tasks(store, 256)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL_MS)
    path = tmp_path / "state.sqlite3"
    claim = store.sync_payload()
    assert payload_total(claim) == 512
    store.close()
    with closing(Store(path, shared_core=SharedCore())) as reopened:
        assert reopened.sync_payload() == claim
        reopened.apply_sync(ack_response(claim, revision=1), claim)
        remainder = reopened.sync_payload()
    assert payload_total(remainder) == 1
    assert remainder["durationOperations"] or remainder["taskOperations"]


@needs_batch_plan
def test_invalid_saved_claim_fails_closed_with_recovery(store):
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL_MS)
    claim = store.sync_payload()
    tampered = deepcopy(claim)
    tampered["durationOperations"].extend(
        {"id": f"r43-fake-{index:05d}"} for index in range(600)
    )
    store.set_meta("pendingSync", tampered)
    with pytest.raises(ValueError, match="aggregate|oversized|recover"):
        store.pending_sync()
    with pytest.raises(ValueError, match="confirm"):
        store.discard_saved_sync_claim()
    assert store.discard_saved_sync_claim(confirmed=True) is True
    assert store.pending_sync() is None
    assert store.sync_payload() == claim


@needs_batch_plan
def test_bootstrap_accepts_exactly_8192(store):
    seed_commands(store, 4096)
    seed_tasks(store, 4096)
    request = store.prepare_resolution({"id": "user-1"}, 0, "merge")
    assert payload_total(request) == 8192


@needs_batch_plan
def test_bootstrap_rejects_8193(store):
    seed_commands(store, 4096)
    seed_tasks(store, 4096)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL_MS)
    with pytest.raises(ValueError, match="oversized|blocked|aggregate|exceed"):
        store.prepare_resolution({"id": "user-1"}, 0, "merge")


@needs_batch_plan
def test_bootstrap_reopen_replays_exactly(store, tmp_path):
    identity = task_from_title("R43 bootstrap task")
    store.queue_task_operation("upsert", identity, now_ms=BASE_WALL_MS)
    path = tmp_path / "state.sqlite3"
    request = store.prepare_resolution({"id": "user-1"}, 0, "merge")
    store.close()
    with closing(Store(path, shared_core=SharedCore())) as reopened:
        assert reopened.prepare_resolution({"id": "user-1"}, 99, "merge") == request


@needs_batch_plan
def test_no_domain_starvation_across_batches(store):
    seed_commands(store, 256)
    seed_tasks(store, 256)
    store.queue_duration_operation("focus", 30 * 60_000, now_ms=BASE_WALL_MS)
    store.set_auto_start_breaks(True, now_ms=BASE_WALL_MS + 1)
    store.set_auto_start_breaks(False, now_ms=BASE_WALL_MS + 2)
    store.set_selected_task_id(None, now_ms=BASE_WALL_MS + 3)
    store.set_selected_task_id(None, now_ms=BASE_WALL_MS + 4)
    loaded = store.load()
    queued = (
        len(loaded["pending"]) + len(loaded["pendingTasks"])
        + len(loaded["pendingDurations"]) + len(loaded["pendingAutoStarts"])
        + len(loaded["pendingSelectedTasks"])
    )
    first = store.sync_payload()
    assert payload_total(first) == 512
    for domain in DOMAINS:
        assert first[domain], domain
    cursor = store.get_meta("batchNextDomain")
    assert cursor in DOMAINS
    store.apply_sync(ack_response(first, revision=1), first)
    second = store.sync_payload()
    assert payload_total(second) == queued - 512
    store.apply_sync(ack_response(second, revision=2), second)
    assert payload_total(store.sync_payload()) == 0


@needs_batch_plan
def test_timer_dependency_barrier_holds_prefix(store):
    seed_commands(store, 3)
    identity = task_from_title("R43 barrier task")
    task = store.queue_task_operation("upsert", identity, now_ms=BASE_WALL_MS)
    store.connection.execute(
        "UPDATE pending_commands SET depends_on_command_id = ? WHERE id = ?",
        ("r43-cmd-00000", "r43-cmd-00001"),
    )
    store.connection.commit()
    payload = store.sync_payload()
    assert [item["id"] for item in payload["commands"]] == ["r43-cmd-00000"]
    assert [item["id"] for item in payload["taskOperations"]] == [task["id"]]
