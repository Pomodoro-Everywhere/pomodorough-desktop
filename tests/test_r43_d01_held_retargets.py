"""Held retarget durability through production WASM/SQLite workspace swaps."""

import json
import sqlite3
import time
from copy import deepcopy

import pytest

from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import record_digest
from pomodorough.storage import Store, utc_timestamp
from test_immutable_reconcile_v2 import MemorySecretStore


def queue_held_retargets(store, targets, auto_start):
    now = int(time.time() * 1000)
    room_id = store.create_iroh_room(bytes(range(32)))
    first, second = [task_from_title(title) for title in ("R43 first", "R43 second")]
    for task in (first, second):
        store.queue_task_operation("upsert", task, now_ms=now)
    store.set_selected_task_id(first["id"], now_ms=now + 1)
    store.set_auto_start_breaks(auto_start, now_ms=now + 2)
    durations = store.load()["settings"]["durationsMs"]
    store.queue_command(
        "start", None, "focus", durations, first["id"], now_ms=now + 10,
    )
    store.upsert_iroh_peer(room_id, "legacy", "ticket", None, None)
    for index, target in enumerate(targets):
        store.set_selected_task_id(
            second["id"] if target == "task" else None, now_ms=now + 20 + index,
        )
    rows = pending_rows(store)
    assert len(rows) == len(targets)
    assert all(json.loads(row["payload"])["type"] == "retarget" for row in rows)
    assert [json.loads(row["payload"])["taskId"] for row in rows] == [
        second["id"] if target == "task" else None for target in targets
    ]
    return room_id, now + 10 + durations["focus"]


def pending_rows(store):
    return [dict(row) for row in store.connection.execute(
        "SELECT * FROM pending_commands ORDER BY device_sequence"
    )]


def held_state(store):
    return {
        "rows": pending_rows(store),
        "physical": store.get_meta("commandPhysicalTimes"),
        "proof": store.delivery_proof(),
        "hlc": store.get_meta("hlc"),
        "sequence": store.get_meta("deviceSequence"),
        "uuid": store.get_meta("lastUuidV7"),
    }


def assert_held_state(store, room_id, before):
    after = held_state(store)
    for key in ("rows", "physical", "proof"):
        assert after[key] == before[key], key
    assert tuple(after["hlc"][key] for key in ("wallMs", "counter")) >= tuple(
        before["hlc"][key] for key in ("wallMs", "counter")
    )
    assert after["sequence"] >= before["sequence"]
    assert after["uuid"] >= before["uuid"]
    workspace = json.loads(store.connection.execute(
        "SELECT workspace FROM iroh_rooms WHERE room_id = ?", (room_id,),
    ).fetchone()["workspace"])
    assert workspace["tables"]["pending_commands"] == before["rows"]
    assert workspace["metadata"]["commandPhysicalTimes"] == before["physical"]
    assert workspace["metadata"]["deliveryProof"] == before["proof"]
    assert workspace["metadata"]["hlc"] == after["hlc"]
    ids = {entry["id"] for entry in store.iroh_inventory(room_id, None, 256)[0]}
    assert ids.isdisjoint(row["id"] for row in before["rows"])


def replace_workspace(store, room_id, transition, expires_at):
    if transition.startswith("expiry"):
        assert store.project_iroh_expiry(expires_at)
        return
    if transition == "inactive_remote":
        store.set_replication_mode("offline")
    record = {
        "domain": "autoStart", "deviceId": "remote-device",
        "operation": {
            "id": "remote-auto-start", "enabled": True,
            "occurredAt": utc_timestamp(1_000),
            "hlcWallMs": 1_000, "hlcCounter": 0,
        },
    }
    assert store.insert_remote_iroh_records(room_id, [record])
    assert not store.insert_remote_iroh_records(room_id, [record])
    if transition == "inactive_remote":
        store.activate_joined_iroh_room(room_id)


def assert_publish_once(store, room_id, before):
    store.upsert_iroh_peer(
        room_id, "legacy", "ticket", None, None, capabilities=["retarget-v1"],
    )
    assert store.capture_local_iroh_records()
    assert not store.capture_local_iroh_records()
    assert pending_rows(store) == []
    inventory = {entry["id"]: entry for entry in store.iroh_inventory(room_id, None, 256)[0]}
    for row in before["rows"]:
        expected = {
            "domain": "timer", "deviceId": store.device_id,
            "operation": json.loads(row["payload"]),
        }
        records = store.iroh_operations(room_id, [{"domain": "timer", "id": row["id"]}])
        assert records == [expected]
        assert inventory[row["id"]]["digest"] == record_digest(expected)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM iroh_records WHERE room_id = ? AND operation_id = ?",
            (room_id, row["id"]),
        ).fetchone()[0] == 1
        assert row["id"] not in store.delivery_proof()["commands"]
        assert row["id"] not in store.get_meta("commandPhysicalTimes", {})


@pytest.mark.parametrize("transition", ["expiry", "expiry_auto", "remote", "inactive_remote"])
@pytest.mark.parametrize("targets", [("task",), (None,), ("task", None, "task")])
def test_held_retargets_survive_workspace_replacement(tmp_path, transition, targets):
    path = tmp_path / "state.sqlite3"
    secrets = MemorySecretStore()
    store = Store(path, iroh_secret_store=secrets)
    try:
        room_id, expires_at = queue_held_retargets(store, targets, transition == "expiry_auto")
        before = deepcopy(held_state(store))
        assert before["physical"]
        assert set(before["proof"]["commands"]) == {row["id"] for row in before["rows"]}
        replace_workspace(store, room_id, transition, expires_at)
        assert_held_state(store, room_id, before)
        store.close()
        store = Store(path, iroh_secret_store=secrets)
        assert_held_state(store, room_id, before)
        assert_publish_once(store, room_id, before)
    finally:
        store.close()


@pytest.mark.parametrize("transition", ["expiry", "remote"])
def test_workspace_restore_failure_rolls_back_held_retargets(tmp_path, transition):
    store = Store(tmp_path / "state.sqlite3", iroh_secret_store=MemorySecretStore())
    try:
        room_id, expires_at = queue_held_retargets(store, ("task", None), False)
        before = deepcopy(held_state(store))
        room_before = tuple(store.connection.execute(
            "SELECT * FROM iroh_rooms WHERE room_id = ?", (room_id,),
        ).fetchone())
        inventory_before = store.iroh_inventory(room_id, None, 256)
        with store.connection:
            store.connection.execute(
                "CREATE TEMP TRIGGER fail_restore BEFORE INSERT ON pending_commands "
                "BEGIN SELECT RAISE(ABORT, 'R43 restore failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="R43 restore failure"):
            replace_workspace(store, room_id, transition, expires_at)
        assert held_state(store) == before
        assert tuple(store.connection.execute(
            "SELECT * FROM iroh_rooms WHERE room_id = ?", (room_id,),
        ).fetchone()) == room_before
        assert store.iroh_inventory(room_id, None, 256) == inventory_before
    finally:
        store.close()
