from __future__ import annotations

import json
import sqlite3

import pytest
from test_iroh_storage import CommitFailingConnection, MemorySecretStore
from test_replication_controller import ControllerHarness, FakeIroh

from pomodorough.controller_outcomes import EmitNotice, LoadState, Render, ShowStatus
from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import room_id_for_secret
from pomodorough.storage import Store


@pytest.fixture
def saved_room_controller(tmp_path):
    secrets = MemorySecretStore()
    harness = ControllerHarness(iroh=FakeIroh())
    harness.store = Store(tmp_path / "saved-room.sqlite3", iroh_secret_store=secrets)
    harness.store.set_replication_mode("offline")
    harness.store.set_user({"id": "local-owner"})
    try:
        yield harness, secrets
    finally:
        harness.store.close()


def clear_controller_events(harness):
    harness.applied.clear()
    harness.mode_selections.clear()
    harness.screens.clear()
    harness.focus_reasons.clear()
    harness.iroh.calls.clear()
    harness.cloud.stops = 0
    harness.cloud.restores = 0


def retain_room(harness, *, seed=1, created_at=100):
    room_id = harness.store.create_iroh_room(
        bytes([seed]) * 32, f"Saved room {seed}", now_ms=created_at
    )
    harness.controller.mode = "iroh"
    harness.controller.replication_mode_changed("offline")
    clear_controller_events(harness)
    assert harness.store.active_iroh_room_id is None
    return room_id


def persisted_state(store):
    return "\n".join(store.connection.iterdump())


def queue_local_workspace(store):
    task = task_from_title("Queued local task")
    store.queue_task_operation("upsert", task, now_ms=1_786_000_000_000)
    store.queue_duration_operation("focus", 1_800_000, now_ms=1_786_000_000_001)
    store.set_auto_start_breaks(True, now_ms=1_786_000_000_002)
    store.set_selected_task_id(task["id"], now_ms=1_786_000_000_003)
    store.queue_command(
        "start", None, "focus", store.load()["settings"]["durationsMs"],
        now_ms=1_786_000_000_004,
    )
    store.sync_payload()
    workspace = store._workspace_storage.capture()
    for table in (
        "pending_commands", "pending_task_operations", "pending_duration_operations",
        "pending_auto_start_operations", "pending_selected_task_operations",
    ):
        assert workspace["tables"][table]
    assert workspace["metadata"]["snapshot"]["user"] == {"id": "local-owner"}
    return workspace


def assert_failed_activation(harness, before, outcome):
    assert persisted_state(harness.store) == before
    assert harness.controller.mode == "offline"
    assert harness.store.replication_mode == "offline"
    assert harness.store.active_iroh_room_id is None
    assert harness.iroh.calls == []
    assert harness.cloud.stops == 0
    assert harness.cloud.restores == 0
    assert harness.mode_selections == ["offline"]
    effects = list(outcome.effects)
    effects.extend(effect for applied in harness.applied for effect in applied.effects)
    assert len(effects) == 1
    assert isinstance(effects[0], EmitNotice)
    assert harness.screens == []
    assert not harness.store.connection.in_transaction
    return effects[0].message


@pytest.mark.parametrize("origin", ["offline", "centralized"])
def test_saved_room_reactivation_preserves_current_return_workspace(
    saved_room_controller, origin
):
    harness, secrets = saved_room_controller
    room_id = retain_room(harness)
    room_workspace = harness.store.connection.execute(
        "SELECT workspace FROM iroh_rooms WHERE room_id = ?", (room_id,)
    ).fetchone()["workspace"]
    harness.store.set_replication_mode(origin)
    harness.controller.mode = origin
    original = queue_local_workspace(harness.store)
    secret_values = dict(secrets.values)
    identity = harness.store.device_id
    outcome = harness.controller.replication_mode_changed("iroh")
    assert outcome.effects == (LoadState(), Render())
    assert harness.controller.mode == harness.store.replication_mode == "iroh"
    assert harness.store.active_iroh_room_id == room_id
    assert harness.store._workspace_storage.capture() == json.loads(room_workspace)
    row = harness.store.connection.execute(
        "SELECT return_workspace FROM iroh_rooms WHERE room_id = ?", (room_id,)
    ).fetchone()
    assert json.loads(row["return_workspace"]) == original
    assert harness.iroh.calls == [("start", room_id, False)]
    assert harness.cloud.stops == 1
    assert harness.screens == harness.applied == []
    harness.controller.replication_mode_changed("offline")
    assert harness.store._workspace_storage.capture() == original
    assert harness.store.device_id == identity
    assert secrets.values == secret_values
    assert harness.iroh.calls[-1] == ("stop",)


def test_saved_room_can_reactivate_after_store_and_controller_restart(
    saved_room_controller,
):
    harness, secrets = saved_room_controller
    room_id = retain_room(harness)
    original = queue_local_workspace(harness.store)
    path = harness.store.path
    harness.store.close()
    harness.store = Store(path, iroh_secret_store=secrets)
    restarted = ControllerHarness(iroh=FakeIroh())
    restarted.store = harness.store
    restarted.controller.mode = harness.store.replication_mode
    restarted.controller.replication_mode_changed("iroh")
    assert restarted.store.active_iroh_room_id == room_id
    assert restarted.iroh.calls == [("start", room_id, False)]
    restarted.controller.replication_mode_changed("offline")
    assert restarted.store._workspace_storage.capture() == original


def test_controller_created_room_retains_records_ids_and_workspace_owner(
    saved_room_controller, monkeypatch
):
    harness, secrets = saved_room_controller
    secret = bytes([4]) * 32
    room_id = room_id_for_secret(secret)
    original = harness.store._workspace_storage.capture()
    monkeypatch.setattr(
        "pomodorough.replication_controller.secrets.token_bytes", lambda _size: secret
    )
    harness.controller.create_iroh_room("Controller-created room")
    assert harness.iroh.calls == [("start", room_id, True)]
    task = task_from_title("Room-only task")
    operation = harness.store.queue_task_operation("upsert", task)
    harness.store.set_selected_task_id(task["id"])
    command = harness.store.queue_command(
        "start", None, "focus", harness.store.load()["settings"]["durationsMs"]
    )
    room_snapshot = harness.store.load()["snapshot"]
    references = [
        {"domain": "task", "id": operation["id"]},
        {"domain": "timer", "id": command["id"]},
    ]
    records = harness.store.iroh_operations(room_id, references)
    assert len(records) == 2
    harness.controller.replication_mode_changed("offline")
    assert harness.store._workspace_storage.capture() == original
    clear_controller_events(harness)
    harness.controller.replication_mode_changed("iroh")
    assert harness.store.active_iroh_room_id == room_id
    assert harness.iroh.calls == [("start", room_id, False)]
    assert harness.store.load()["snapshot"] == room_snapshot
    assert harness.store.load()["snapshot"]["user"] is None
    assert harness.store.iroh_operations(room_id, references) == records
    assert harness.store.iroh_room_secret(room_id) == secret
    harness.controller.replication_mode_changed("offline")
    assert harness.store._workspace_storage.capture() == original
    assert len(secrets.values) == 1


@pytest.mark.parametrize("timestamps", [(100, 200), (200, 100), (100, 100)])
def test_saved_room_selection_matches_store_with_multiple_and_incomplete_rooms(
    saved_room_controller, timestamps
):
    harness, secrets = saved_room_controller
    room_ids = [
        retain_room(harness, seed=seed, created_at=created_at)
        for seed, created_at in enumerate(timestamps, start=1)
    ]
    pending_secret = bytes([3]) * 32
    pending_id = room_id_for_secret(pending_secret)
    harness.store.prepare_iroh_join(
        pending_id, pending_secret, "Incomplete", "endpoint", "ticket", now_ms=300
    )
    harness.store.set_replication_mode("iroh")
    expected = harness.store.active_iroh_room_id
    assert expected in room_ids
    harness.store.set_replication_mode("offline")
    before = persisted_state(harness.store)
    assert harness.controller.iroh_transition_ready().value is True
    assert persisted_state(harness.store) == before
    harness.controller.replication_mode_changed("iroh")
    assert harness.store.active_iroh_room_id == expected
    assert harness.iroh.calls == [("start", expected, False)]
    assert harness.store.iroh_room(pending_id)["operationCount"] == 0
    assert harness.store.iroh_room_secret(pending_id) == pending_secret
    assert len(secrets.values) == 4


@pytest.mark.parametrize("room_state", ["absent", "incomplete", "missing-genesis"])
def test_first_room_guidance_preserves_workspace_without_eligible_saved_room(
    saved_room_controller, room_state
):
    harness, _secrets = saved_room_controller
    if room_state == "incomplete":
        secret = bytes([2]) * 32
        harness.store.prepare_iroh_join(
            room_id_for_secret(secret), secret, "Pending", "endpoint", "ticket"
        )
    elif room_state == "missing-genesis":
        room_id = retain_room(harness)
        harness.store.connection.execute(
            "DELETE FROM iroh_records WHERE room_id = ? AND domain = 'genesis'",
            (room_id,),
        )
        harness.store.connection.commit()
    before = persisted_state(harness.store)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert outcome.effects == ()
    assert persisted_state(harness.store) == before
    assert harness.controller.mode == harness.store.replication_mode == "offline"
    assert harness.iroh.calls == []
    assert harness.screens == [(3, False)]
    assert len(harness.focus_reasons) == 1
    assert harness.applied[-1].effects == (
        ShowStatus("iroh.first_room_guidance", 10_000),
    )


@pytest.mark.parametrize("secret", [None, b"", b"short", bytes([9]) * 32])
def test_latest_saved_room_invalid_secret_cannot_fall_back_or_activate(
    saved_room_controller, secret
):
    harness, secrets = saved_room_controller
    retain_room(harness, seed=1, created_at=100)
    latest = retain_room(harness, seed=2, created_at=200)
    queue_local_workspace(harness.store)
    key = harness.store._room_secret_key(latest)
    if secret is None:
        secrets.delete(key)
    else:
        secrets.save(key, secret)
    secret_values = dict(secrets.values)
    before = persisted_state(harness.store)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert "secret" in assert_failed_activation(harness, before, outcome).lower()
    assert secrets.values == secret_values


def test_saved_room_secret_load_error_keeps_current_workspace(
    saved_room_controller, monkeypatch
):
    harness, secrets = saved_room_controller
    retain_room(harness)
    before = persisted_state(harness.store)

    def fail_load(_key):
        raise OSError("Injected secret read failure")

    monkeypatch.setattr(secrets, "load", fail_load)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert assert_failed_activation(harness, before, outcome) == (
        "Injected secret read failure"
    )


@pytest.mark.parametrize("conflict", ['{"reason":"immutable conflict"}', "{", ""])
def test_latest_saved_room_conflict_never_falls_back_to_older_room(
    saved_room_controller, conflict
):
    harness, _secrets = saved_room_controller
    retain_room(harness, seed=1, created_at=100)
    latest = retain_room(harness, seed=2, created_at=200)
    harness.store.connection.execute(
        "UPDATE iroh_rooms SET conflict = ? WHERE room_id = ?", (conflict, latest)
    )
    harness.store.connection.commit()
    before = persisted_state(harness.store)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert "repair" in assert_failed_activation(harness, before, outcome)


@pytest.mark.parametrize("workspace", [
    "{", "[]", '{}', '{"metadata":{},"tables":[]}',
    '{"metadata":{},"tables":{"pending_commands":[{}]}}',
])
def test_corrupt_saved_workspace_rolls_back_activation_and_return_capture(
    saved_room_controller, workspace
):
    harness, _secrets = saved_room_controller
    room_id = retain_room(harness)
    queue_local_workspace(harness.store)
    harness.store.connection.execute(
        "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?", (workspace, room_id)
    )
    harness.store.connection.commit()
    before = persisted_state(harness.store)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert_failed_activation(harness, before, outcome)


@pytest.mark.parametrize("failure", [OSError, ValueError, sqlite3.OperationalError])
def test_saved_room_activation_rolls_back_after_workspace_restore(
    saved_room_controller, monkeypatch, failure
):
    harness, _secrets = saved_room_controller
    retain_room(harness)
    queue_local_workspace(harness.store)
    workspace = harness.store._workspace_storage
    restore = workspace.restore
    before = persisted_state(harness.store)

    def fail_after_restore(saved):
        restore(saved)
        raise failure("Injected restore failure")

    monkeypatch.setattr(workspace, "restore", fail_after_restore)
    outcome = harness.controller.replication_mode_changed("iroh")
    assert assert_failed_activation(harness, before, outcome) == "Injected restore failure"


def test_saved_room_activation_commit_failure_rolls_back_every_database_change(
    saved_room_controller,
):
    harness, secrets = saved_room_controller
    retain_room(harness)
    queue_local_workspace(harness.store)
    before = persisted_state(harness.store)
    secret_values = dict(secrets.values)
    connection = harness.store.connection
    harness.store.connection = CommitFailingConnection(connection)
    try:
        outcome = harness.controller.replication_mode_changed("iroh")
        assert assert_failed_activation(harness, before, outcome) == "forced commit failure"
        assert secrets.values == secret_values
    finally:
        harness.store.connection = connection


@pytest.mark.parametrize("blocked_by", ["packaging", "availability", "cloud-busy"])
def test_saved_room_readiness_keeps_packaging_availability_and_cloud_guards(
    saved_room_controller, blocked_by
):
    harness, _secrets = saved_room_controller
    retain_room(harness)
    iroh = harness.iroh
    if blocked_by == "packaging":
        harness.iroh = None
    elif blocked_by == "availability":
        iroh.available = False
    else:
        harness.store.set_replication_mode("centralized")
        harness.controller.mode = "centralized"
        harness.cloud.busy = True
    before = persisted_state(harness.store)
    assert harness.controller.replication_transition_ready("iroh").value is False
    harness.controller.replication_mode_changed("iroh")
    assert persisted_state(harness.store) == before
    assert iroh.calls == []
    assert harness.screens == []
    assert harness.cloud.stops == harness.cloud.restores == 0
    assert harness.controller.mode == harness.store.replication_mode
