from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from test_iroh_storage import CommitFailingConnection, MemorySecretStore
from test_replication_controller import ControllerHarness, FakeIroh

from pomodorough.controller_outcomes import EmitNotice, LoadState, Render
from pomodorough.core import task_from_title
from pomodorough.iroh_network import EndpointKeyStore, IrohService
from pomodorough.iroh_protocol import RoomInvite, room_id_for_secret
from pomodorough.network_screen import NetworkScreen
from pomodorough.storage import Store


@pytest.fixture
def rig(tmp_path):
    vault = MemorySecretStore()
    harness = ControllerHarness(iroh=FakeIroh())
    harness.store = Store(tmp_path / "transitions.sqlite3", iroh_secret_store=vault)
    harness.store.set_replication_mode("offline")
    harness.store.set_user({"id": "original-owner"})
    harness.vault = vault
    try:
        yield harness
    finally:
        harness.store.close()


def seed_workspace(store):
    task = task_from_title("All five queues survive")
    store.queue_task_operation("upsert", task, now_ms=1_786_000_000_000)
    store.queue_duration_operation("focus", 1_860_000, now_ms=1_786_000_000_001)
    store.set_auto_start_breaks(True, now_ms=1_786_000_000_002)
    store.set_selected_task_id(task["id"], now_ms=1_786_000_000_003)
    store.queue_command(
        "start", None, "focus", store.load()["settings"]["durationsMs"],
        now_ms=1_786_000_000_004,
    )
    store.sync_payload()
    workspace = store._workspace_storage.capture()
    assert sum(bool(rows) for rows in workspace["tables"].values()) == 5
    return workspace


def retain_room(rig):
    room = rig.store.create_iroh_room(bytes([11]) * 32, "Retained room")
    rig.store.queue_task_operation("upsert", task_from_title("Room-only task"))
    rig.controller.mode = "iroh"
    rig.controller.replication_mode_changed("offline")
    return room


def invite_for(seed=44):
    secret = bytes([seed]) * 32
    return RoomInvite(room_id_for_secret(secret), "fake-ticket", "fake-peer", secret)


def add_genesis(store, invite):
    record = {
        "domain": "genesis", "deviceId": "remote-test-device",
        "operation": {
            "canonicalTimer": None, "history": [], "tasks": [],
            "durationsMs": store.load()["settings"]["durationsMs"],
            "autoStartBreaks": False, "selectedTaskId": None,
            "hlcWallMs": 0, "hlcCounter": 0,
        },
    }
    store.insert_remote_iroh_records(invite.room_id, [record])


def begin_join(rig, monkeypatch, invite=None):
    invite = invite or invite_for()
    monkeypatch.setattr("pomodorough.replication_controller.parse_invite", lambda _: invite)
    rig.controller.join_iroh_room("injected invite")
    return invite


def persisted(rig):
    return tuple(rig.store.connection.iterdump()), dict(rig.vault.values)


@pytest.mark.parametrize("action", ["create", "saved", "join", "restore", "activate"])
@pytest.mark.parametrize("origin", ["offline", "centralized"])
def test_pending_join_blocks_competing_workspace_mutations(rig, monkeypatch, action, origin):
    saved = retain_room(rig)
    rig.controller.replication_mode_changed(origin)
    original = seed_workspace(rig.store)
    invite = begin_join(rig, monkeypatch)
    before = persisted(rig)
    actions = {
        "create": lambda: rig.controller.create_iroh_room("Nested room"),
        "saved": lambda: rig.controller.replication_mode_changed(2),
        "join": lambda: begin_join(rig, monkeypatch, invite_for(55)),
        "restore": rig.controller.restore_replication,
        "activate": lambda: rig.controller.activate_replication_mode(origin, "iroh"),
    }
    actions[action]()
    assert persisted(rig) == before
    assert not rig.controller.iroh_transition_ready().value
    assert not rig.controller.replication_transition_ready("iroh").value
    add_genesis(rig.store, invite)
    rig.store.activate_joined_iroh_room(invite.room_id)
    rig.controller.iroh_joined()
    rig.controller.replication_mode_changed(origin)
    assert rig.store._workspace_storage.capture() == original
    assert rig.store.iroh_room(saved) is not None
    assert rig.vault.values == before[1]
    rig.controller.replication_mode_changed("iroh")
    assert rig.controller.mode == "iroh"


class DelayedTransport:
    def __init__(self, rig, monkeypatch):
        self.rig = rig
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.callbacks = []
        self.service = IrohService(
            rig.store.path, rig.store.device_id,
            key_store=EndpointKeyStore(rig.vault),
        )
        self.service._store = rig.store
        self.service._session_lock = asyncio.Lock()
        rig.iroh = self.service
        monkeypatch.setattr(self.service, "availability", lambda: (True, "test"))
        monkeypatch.setattr(self.service, "_ensure_loop", asyncio.get_running_loop)
        monkeypatch.setattr(self.service, "_start_room", self.start)
        monkeypatch.setattr(self.service, "_stop_endpoint", AsyncMock())
        monkeypatch.setattr(self.service, "_exchange", self.exchange)
        monkeypatch.setattr(self.service, "_emit_details", lambda: None)
        address = SimpleNamespace(id=lambda: "fake-peer")
        ticket = SimpleNamespace(endpoint_addr=lambda: address)
        native = SimpleNamespace(EndpointTicket=SimpleNamespace(from_string=lambda _: ticket))
        monkeypatch.setitem(sys.modules, "iroh", native)
        direct = Qt.ConnectionType.DirectConnection
        self.service.joined.connect(self.queue_joined, direct)
        self.service.failure.connect(self.queue_failure, direct)

    def queue_joined(self):
        self.callbacks.append(self.rig.controller.iroh_joined)

    def queue_failure(self, message):
        self.callbacks.append(partial(self.rig.controller.iroh_failure, message))

    async def start(self, room_id, *, emit_invite):
        self.service._room_id = room_id
        connection = SimpleNamespace(remote_id=lambda: "fake-peer")
        self.service._endpoint = SimpleNamespace(connect=AsyncMock(return_value=connection))

    async def exchange(self, connection):
        self.entered.set()
        await self.release.wait()

    async def begin(self, monkeypatch, invite=None):
        invite = begin_join(self.rig, monkeypatch, invite)
        await asyncio.wait_for(self.entered.wait(), 2)
        add_genesis(self.rig.store, invite)
        return invite

    async def finish(self):
        pending = tuple(self.service._operations)
        self.release.set()
        for future in pending:
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), 2)
            except (sqlite3.Error, ValueError):
                pass
        await asyncio.sleep(0)

    def deliver(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


@pytest.mark.parametrize("action", ["offline", "centralized", "leave", "close", "shutdown"])
def test_delayed_native_completion_cannot_reactivate_cancelled_join(rig, monkeypatch, action):
    asyncio.run(cancelled_join_scenario(rig, monkeypatch, action))


async def cancelled_join_scenario(rig, monkeypatch, action):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    invite = await transport.begin(monkeypatch)
    before = persisted(rig)
    if action == "leave":
        rig.controller.leave_iroh_room()
    elif action == "close":
        rig.closed = True
    elif action == "shutdown":
        transport.service.shutdown()
    else:
        rig.controller.replication_mode_changed(action)
    await transport.finish()
    transport.deliver()
    assert rig.store.active_iroh_room_id is None
    assert rig.store._workspace_storage.capture() == original
    assert rig.store.iroh_room(invite.room_id) is not None
    assert rig.vault.values == before[1]
    if action not in {"close", "shutdown"}:
        assert not rig.controller.iroh_join_pending
        assert rig.store.replication_mode == ("centralized" if action == "centralized" else "offline")
        begin_join(rig, monkeypatch, invite_for(55))
        await transport.finish()
        transport.deliver()


@pytest.mark.parametrize("action", ["offline", "centralized", "leave"])
def test_completion_winning_before_cancel_restores_original_workspace(rig, monkeypatch, action):
    asyncio.run(completion_wins_scenario(rig, monkeypatch, action))


async def completion_wins_scenario(rig, monkeypatch, action):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    invite = await transport.begin(monkeypatch)
    await transport.finish()
    assert rig.store.active_iroh_room_id == invite.room_id
    assert rig.controller.iroh_join_pending
    if action == "leave":
        rig.controller.leave_iroh_room()
    else:
        rig.controller.replication_mode_changed(action)
    transport.deliver()
    assert rig.store.active_iroh_room_id is None
    assert rig.store._workspace_storage.capture() == original
    assert not rig.controller.iroh_join_pending


def test_activation_commit_failure_releases_guard_and_allows_retry(rig, monkeypatch):
    asyncio.run(commit_failure_scenario(rig, monkeypatch))


async def commit_failure_scenario(rig, monkeypatch):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    await transport.begin(monkeypatch)
    before = persisted(rig)
    connection = rig.store.connection
    rig.store.connection = CommitFailingConnection(connection)
    try:
        await transport.finish()
        transport.deliver()
    finally:
        rig.store.connection = connection
    assert persisted(rig) == before
    assert not rig.controller.iroh_join_pending
    assert not connection.in_transaction
    assert rig.store._workspace_storage.capture() == original
    begin_join(rig, monkeypatch)
    await transport.finish()
    transport.deliver()
    assert rig.controller.mode == "iroh"
    rig.controller.replication_mode_changed("offline")
    assert rig.store._workspace_storage.capture() == original


@pytest.mark.parametrize("action", ["create", "join", "leave"])
def test_synchronous_storage_failure_does_not_latch_transition(rig, monkeypatch, action):
    original = seed_workspace(rig.store)
    before = persisted(rig)
    connection = rig.store.connection
    rig.store.connection = CommitFailingConnection(connection)
    monkeypatch.setattr("pomodorough.replication_controller.QMessageBox.warning", lambda *args: 16384)
    actions = {
        "create": lambda: rig.controller.create_iroh_room("Commit failure"),
        "join": lambda: begin_join(rig, monkeypatch),
        "leave": rig.controller.leave_iroh_room,
    }
    try:
        actions[action]()
    finally:
        rig.store.connection = connection
    assert persisted(rig) == before
    assert not rig.controller.iroh_join_pending
    assert not rig.controller._transition_busy
    rig.controller.create_iroh_room("Retry succeeds")
    rig.controller.replication_mode_changed("offline")
    assert rig.store._workspace_storage.capture() == original


def test_join_submission_failure_restores_cloud_and_allows_retry(rig, monkeypatch):
    rig.controller.replication_mode_changed("centralized")
    original = seed_workspace(rig.store)
    def fail(invite):
        raise RuntimeError("submission failed")
    monkeypatch.setattr(rig.iroh, "join_room", fail)
    begin_join(rig, monkeypatch)
    assert not rig.controller.iroh_join_pending
    assert rig.cloud.restores == 2
    assert rig.store._workspace_storage.capture() == original
    rig.controller.create_iroh_room("Retry")
    assert rig.controller.mode == "iroh"


def test_reentrant_transition_during_prepare_cannot_create_nested_room(rig, monkeypatch):
    original = seed_workspace(rig.store)
    prepare = rig.store.prepare_iroh_join
    def reenter(*arguments):
        outcome = rig.controller.create_iroh_room("Reentrant room")
        assert isinstance(outcome.effects[0], EmitNotice)
        return prepare(*arguments)
    monkeypatch.setattr(rig.store, "prepare_iroh_join", reenter)
    begin_join(rig, monkeypatch)
    assert rig.store.active_iroh_room_id is None
    assert rig.store._workspace_storage.capture() == original


def test_mutation_boundary_holds_lock_through_sqlite_commit(rig, monkeypatch):
    service = IrohService(rig.store.path, rig.store.device_id, key_store=EndpointKeyStore(rig.vault))
    service._store = rig.store
    invite = begin_join(rig, monkeypatch)
    add_genesis(rig.store, invite)
    entered, acquired = threading.Event(), threading.Event()
    def competing_transition():
        entered.set()
        with service.workspace_lock:
            acquired.set()
    connection = rig.store.connection
    workers = []
    def guarded_commit():
        worker = threading.Thread(target=competing_transition)
        workers.append(worker)
        worker.start()
        assert entered.wait(2)
        assert not acquired.wait(0.05)
        return connection.commit()
    rig.store.connection = CommitObservedConnection(connection, guarded_commit)
    monkeypatch.setattr(service, "_emit_details", lambda: None)
    try:
        service._complete_join(invite.room_id, 0, lambda: True, ("offline", None))
    finally:
        rig.store.connection = connection
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert acquired.is_set()
    row = rig.store.connection.execute(
        "SELECT return_workspace FROM iroh_rooms WHERE room_id=?", (invite.room_id,),
    ).fetchone()
    assert json.loads(row[0])["metadata"]["snapshot"]["user"] == {"id": "original-owner"}


class CommitObservedConnection:
    def __init__(self, connection, commit):
        self.connection = connection
        self.commit = commit

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_stale_queued_signals_cannot_finish_new_join(rig, monkeypatch):
    asyncio.run(stale_signal_scenario(rig, monkeypatch))


async def stale_signal_scenario(rig, monkeypatch):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    invite = await transport.begin(monkeypatch)
    rig.controller.replication_mode_changed("offline")
    await transport.finish()
    transport.entered.clear()
    transport.release.clear()
    await transport.begin(monkeypatch, invite)
    rig.controller.iroh_joined()
    rig.controller.iroh_failure("obsolete failure")
    assert rig.controller.iroh_join_pending
    assert rig.store.active_iroh_room_id is None
    await transport.finish()
    rig.controller.iroh_failure("obsolete failure after new commit")
    assert rig.controller.iroh_join_pending
    transport.deliver()
    assert not rig.controller.iroh_join_pending
    assert rig.controller.mode == "iroh"
    rig.controller.replication_mode_changed("offline")
    rig.controller.iroh_joined()
    assert rig.controller.mode == "offline"
    assert rig.store._workspace_storage.capture() == original


@pytest.mark.parametrize("action", ["stop", "start", "shutdown"])
def test_queued_join_is_fenced_before_transport_starts(rig, monkeypatch, action):
    asyncio.run(queued_join_scenario(rig, monkeypatch, action))


async def queued_join_scenario(rig, monkeypatch, action):
    transport = DelayedTransport(rig, monkeypatch)
    begin_join(rig, monkeypatch)
    before = persisted(rig)
    if action == "stop":
        transport.service.stop()
    elif action == "shutdown":
        transport.service.shutdown()
    else:
        transport.service.start_room("replacement-room")
    await transport.finish()
    assert not transport.entered.is_set()
    assert persisted(rig) == before


@pytest.mark.parametrize("failure", ["no-loop", "submission", "cancelled", "session-lock", "closed"])
def test_native_dispatch_errors_do_not_leave_pending_flag(rig, monkeypatch, failure):
    asyncio.run(dispatch_failure_scenario(rig, monkeypatch, failure))


async def dispatch_failure_scenario(rig, monkeypatch, failure):
    transport = DelayedTransport(rig, monkeypatch)
    if failure == "cancelled":
        monkeypatch.setattr(transport.service, "_exchange", AsyncMock(side_effect=asyncio.CancelledError))
    elif failure == "session-lock":
        transport.service._session_lock = None
    elif failure == "closed":
        transport.service.shutdown()
    else:
        def fail_loop():
            from pomodorough.iroh_protocol import IrohProtocolError
            raise IrohProtocolError("no loop") if failure == "no-loop" else RuntimeError("submission")
        monkeypatch.setattr(transport.service, "_ensure_loop", fail_loop)
    begin_join(rig, monkeypatch)
    if failure == "cancelled":
        for future in tuple(transport.service._operations):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wrap_future(future)
        transport.service._stop_endpoint.assert_awaited_once_with()
    elif failure == "session-lock":
        await transport.finish()
    transport.deliver()
    assert not rig.controller.iroh_join_pending
    assert not transport.service.join_pending
    assert not rig.controller._transition_busy


@pytest.mark.parametrize("action", ["create", "join", "saved", "leave", "restore", "joined", "failure"])
def test_closed_controller_never_reads_or_mutates_closed_store(rig, monkeypatch, action):
    begin_join(rig, monkeypatch)
    rig.closed = True
    rig.store.close()
    actions = {
        "create": rig.controller.create_iroh_room,
        "join": lambda: begin_join(rig, monkeypatch),
        "saved": lambda: rig.controller.replication_mode_changed("iroh"),
        "leave": rig.controller.leave_iroh_room,
        "restore": rig.controller.restore_replication,
        "joined": rig.controller.iroh_joined,
        "failure": lambda: rig.controller.iroh_failure("late error"),
    }
    actions[action]()
    assert not rig.controller.iroh_join_pending


@pytest.mark.parametrize("available", [False, True])
def test_join_controls_expose_cancel_without_qt_application(rig, monkeypatch, available):
    begin_join(rig, monkeypatch)
    screen = Mock()
    screen.strings = rig.context().strings
    screen._render_controls = partial(NetworkScreen._render_controls, screen)
    NetworkScreen.render(
        screen, replication_mode="offline", iroh_status="OPENING ROUTE",
        iroh_details=rig.controller.iroh_details, invite="", room=None,
        available=available, unavailable_reason="", cloud_authenticated=False,
        cloud_deleting_account=False,
    )
    screen.iroh_panel.setEnabled.assert_called_with(True)
    screen.create_room_button.setEnabled.assert_called_with(False)
    screen.join_room_button.setEnabled.assert_called_with(False)
    screen.leave_room_button.setEnabled.assert_called_with(True)
    screen.leave_room_button.setText.assert_called_with("action.cancel")
    assert rig.controller.iroh_details["joinPending"]


@pytest.mark.parametrize("cancel", [False, True])
def test_resumed_native_join_obeys_completion_fence(rig, monkeypatch, cancel):
    asyncio.run(resumed_join_scenario(rig, monkeypatch, cancel))


async def resumed_join_scenario(rig, monkeypatch, cancel):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    monkeypatch.setattr(
        transport.service, "join_room", lambda invite: transport.service.resume_join(invite.room_id),
    )
    invite = await transport.begin(monkeypatch)
    if cancel:
        rig.controller.replication_mode_changed("offline")
    await transport.finish()
    transport.deliver()
    assert rig.store.active_iroh_room_id == (None if cancel else invite.room_id)
    assert not rig.controller.iroh_join_pending
    rig.controller.replication_mode_changed("offline")
    assert rig.store._workspace_storage.capture() == original


def test_changed_workspace_origin_rejects_completion_without_latching(rig, monkeypatch):
    asyncio.run(changed_origin_scenario(rig, monkeypatch))


async def changed_origin_scenario(rig, monkeypatch):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    await transport.begin(monkeypatch)
    rig.store.set_replication_mode("centralized")
    before = persisted(rig)
    await transport.finish()
    transport.deliver()
    assert persisted(rig) == before
    assert not rig.controller.iroh_join_pending
    assert rig.store._workspace_storage.capture() == original


def test_controller_cancellation_waits_for_worker_commit(rig, monkeypatch):
    original = seed_workspace(rig.store)
    service = IrohService(rig.store.path, rig.store.device_id, key_store=EndpointKeyStore(rig.vault))
    rig.iroh = service
    monkeypatch.setattr(service, "availability", lambda: (True, "test"))
    monkeypatch.setattr(service, "join_room", lambda invite: None)
    monkeypatch.setattr(service, "_emit_details", lambda: None)
    invite = begin_join(rig, monkeypatch)
    add_genesis(rig.store, invite)
    committing, cancel_started, release = (threading.Event() for _ in range(3))
    errors = []
    worker = threading.Thread(
        target=complete_on_worker,
        args=(rig, service, invite, committing, release, errors),
    )
    unblock = threading.Thread(target=release_worker_commit, args=(cancel_started, release))
    worker.start()
    try:
        assert committing.wait(2)
        acquired = service.workspace_lock.acquire(blocking=False)
        if acquired:
            service.workspace_lock.release()
        assert not acquired
        unblock.start()
        cancel_started.set()
        rig.controller.replication_mode_changed("offline")
    finally:
        release.set()
        worker.join(2)
        if unblock.ident is not None:
            unblock.join(2)
    assert not worker.is_alive()
    assert not errors
    assert not rig.controller.iroh_join_pending
    assert rig.store._workspace_storage.capture() == original
    assert rig.store.iroh_room(invite.room_id) is not None


def complete_on_worker(rig, service, invite, committing, release, errors):
    store = Store(rig.store.path, iroh_secret_store=rig.vault)
    service._store = store
    connection = store.connection
    def paused_commit():
        committing.set()
        assert release.wait(2)
        return connection.commit()
    store.connection = CommitObservedConnection(connection, paused_commit)
    try:
        service._complete_join(invite.room_id, 0, service.join_allowed, ("offline", None))
    except Exception as error:
        errors.append(error)
    finally:
        store.connection = connection
        store.close()
        service._store = None


def release_worker_commit(cancel_started, release):
    if cancel_started.wait(2):
        release.wait(0.05)
    release.set()


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("action", ["offline", "centralized", "leave"])
def test_cancellation_commit_failure_remains_retryable(rig, monkeypatch, completed, action):
    asyncio.run(cancellation_failure_scenario(rig, monkeypatch, completed, action))


async def cancellation_failure_scenario(rig, monkeypatch, completed, action):
    original = seed_workspace(rig.store)
    transport = DelayedTransport(rig, monkeypatch)
    await transport.begin(monkeypatch)
    if completed:
        await transport.finish()
    before = persisted(rig)
    connection = rig.store.connection
    rig.store.connection = CommitFailingConnection(connection)
    try:
        outcome = (rig.controller.leave_iroh_room() if action == "leave"
                   else rig.controller.replication_mode_changed(action))
    finally:
        rig.store.connection = connection
    await transport.finish()
    transport.deliver()
    assert persisted(rig) == before
    assert not rig.controller.iroh_join_pending
    assert not rig.controller._transition_busy
    assert rig.controller.mode == rig.store.replication_mode
    assert tuple(type(effect) for effect in outcome.effects) == (EmitNotice, LoadState, Render)
    rig.controller.replication_mode_changed("offline")
    assert rig.store._workspace_storage.capture() == original


def test_cancellation_while_waiting_for_session_lock_releases_guard(rig, monkeypatch):
    asyncio.run(cancel_waiter_scenario(rig, monkeypatch))


async def cancel_waiter_scenario(rig, monkeypatch):
    transport = DelayedTransport(rig, monkeypatch)
    async with transport.service._session_lock:
        begin_join(rig, monkeypatch)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pending = tuple(transport.service._operations)
        transport.service._cancel_operations()
        for future in pending:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wrap_future(future)
        await asyncio.sleep(0)
    transport.deliver()
    assert not rig.controller.iroh_join_pending
    assert not transport.entered.is_set()
    transport.service._stop_endpoint.assert_not_awaited()
