from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import room_id_for_secret
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from test_storage_replication_modules import MemorySecretStore


ROOM_SECRET = bytes(range(32))
ROOM_ID = room_id_for_secret(ROOM_SECRET)


@pytest.fixture
def connections(tmp_path):
    secrets = MemorySecretStore()
    creator = Store(tmp_path / "state.sqlite3", iroh_secret_store=secrets)
    writer = Store(creator.path, iroh_secret_store=secrets)
    creator.set_user({"id": "original-account"})
    writer.connection.execute("PRAGMA busy_timeout=0")
    try:
        yield creator, writer, secrets
    finally:
        writer.close()
        creator.close()


def commit_operation(writer, domain):
    if domain == "task":
        return writer.queue_task_operation(
            "upsert", task_from_title("Concurrent committed task")
        )
    return LocalTimer(writer).issue("start")


def assert_projected_operation(snapshot, operation, domain):
    if domain == "task":
        assert snapshot["tasks"] == [
            {"id": operation["taskId"], "title": operation["title"]}
        ]
    else:
        assert snapshot["canonicalTimer"] is not None
        assert snapshot["canonicalTimer"]["id"] == operation["timerId"]
        assert snapshot["canonicalTimer"]["status"] == "running"


def assert_durable_workspaces(connections, original, operation, domain):
    creator, writer, secrets = connections
    writer.close()
    creator.close()
    reopened = Store(creator.path, iroh_secret_store=secrets)
    try:
        assert reopened.active_iroh_room_id == ROOM_ID
        assert_projected_operation(reopened.load()["snapshot"], operation, domain)
        reopened.leave_iroh_room()
        assert reopened._workspace_storage.capture() == original
        assert reopened.load()["snapshot"]["user"] == {"id": "original-account"}
    finally:
        reopened.close()
    reopened = Store(creator.path, iroh_secret_store=secrets)
    try:
        assert reopened.replication_mode == "offline"
        assert reopened._workspace_storage.capture() == original
        reopened.set_replication_mode("iroh")
        assert_projected_operation(reopened.load()["snapshot"], operation, domain)
    finally:
        reopened.close()


@pytest.mark.parametrize("domain", ["task", "timer"])
def test_commit_before_room_transaction_survives_both_workspaces(
    connections, monkeypatch, domain
):
    creator, writer, _secrets = connections
    lifecycle = creator._replication_storage._rooms
    dependencies = lifecycle._dependencies
    committed = {}

    @contextmanager
    def commit_before_begin():
        committed["operation"] = commit_operation(writer, domain)
        committed["workspace"] = writer._workspace_storage.capture()
        assert not writer.connection.in_transaction
        with dependencies.immediate_transaction():
            yield

    monkeypatch.setattr(lifecycle, "_dependencies", replace(
        dependencies, immediate_transaction=commit_before_begin
    ))
    assert creator.create_iroh_room(ROOM_SECRET) == ROOM_ID
    snapshot = writer.load()["snapshot"]
    assert snapshot["user"] is None
    assert_projected_operation(snapshot, committed["operation"], domain)
    row = writer.connection.execute(
        "SELECT return_workspace FROM iroh_rooms WHERE room_id = ?", (ROOM_ID,)
    ).fetchone()
    assert json.loads(row["return_workspace"]) == committed["workspace"]
    assert_durable_workspaces(
        connections, committed["workspace"], committed["operation"], domain
    )


def creation_boundary(creator, secrets, boundary):
    projection = creator._replication_storage._projection
    workspace = creator._workspace_storage
    return {
        "genesis": (projection, "projected_local_genesis"),
        "capture": (workspace, "capture"),
        "workspace": (projection, "empty_workspace"),
        "install": (workspace, "restore"),
        "secret": (secrets, "save"),
    }[boundary]


@pytest.mark.parametrize("domain", ["task", "timer"])
@pytest.mark.parametrize("boundary", ["genesis", "capture", "workspace", "install", "secret"])
def test_writers_excluded_until_room_commit_then_owned_by_room(
    connections, monkeypatch, domain, boundary
):
    creator, writer, secrets = connections
    original_workspace = writer._workspace_storage.capture()
    target, name = creation_boundary(creator, secrets, boundary)
    original = getattr(target, name)
    attempts = []
    statements = []

    def attempt_after_boundary(*args, **kwargs):
        result = original(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            commit_operation(writer, domain)
        attempts.append(boundary)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(target, name, attempt_after_boundary)
        creator.connection.set_trace_callback(statements.append)
        creator.create_iroh_room(ROOM_SECRET)
        creator.connection.set_trace_callback(None)
    assert attempts
    assert [sql for sql in statements if sql.split()[0] in {
        "BEGIN", "COMMIT", "ROLLBACK"
    }] == ["BEGIN IMMEDIATE", "COMMIT"]
    operation = commit_operation(writer, domain)
    assert not writer.connection.in_transaction
    assert_projected_operation(creator.load()["snapshot"], operation, domain)
    assert_durable_workspaces(connections, original_workspace, operation, domain)


def inject_creation_failure(creator, secrets, boundary, monkeypatch):
    if boundary == "commit":
        def reject_commit(action, operation, _argument, _database, _trigger):
            if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        creator.connection.set_authorizer(reject_commit)
        return
    target, name = creation_boundary(creator, secrets, boundary)
    original = getattr(target, name)

    def fail_after_boundary(*args, **kwargs):
        monkeypatch.setattr(target, name, original)
        original(*args, **kwargs)
        raise RuntimeError("injected creation failure")

    monkeypatch.setattr(target, name, fail_after_boundary)


@pytest.mark.parametrize("previous_secret", [None, b"preexisting secret value"])
@pytest.mark.parametrize("boundary", ["genesis", "capture", "workspace", "install", "secret", "commit"])
def test_creation_failure_rolls_back_database_and_secret(
    connections, monkeypatch, boundary, previous_secret
):
    creator, writer, secrets = connections
    commit_operation(writer, "task")
    timer_operation = commit_operation(writer, "timer")
    original_workspace = writer._workspace_storage.capture()
    secret_key = creator._room_secret_key(ROOM_ID)
    if previous_secret is not None:
        secrets.save(secret_key, previous_secret)
    original_secrets = dict(secrets.values)
    with monkeypatch.context() as scoped:
        inject_creation_failure(creator, secrets, boundary, scoped)
        try:
            expected = "not authorized" if boundary == "commit" else "injected creation failure"
            with pytest.raises((sqlite3.DatabaseError, RuntimeError), match=expected):
                creator.create_iroh_room(ROOM_SECRET)
        finally:
            creator.connection.set_authorizer(None)
    assert not creator.connection.in_transaction
    assert writer._workspace_storage.capture() == original_workspace
    assert writer.replication_mode == "centralized"
    assert writer.active_iroh_room_id is None
    assert writer.connection.execute("SELECT COUNT(*) FROM iroh_rooms").fetchone()[0] == 0
    assert writer.connection.execute("SELECT COUNT(*) FROM iroh_records").fetchone()[0] == 0
    assert secrets.values == original_secrets
    assert creator.create_iroh_room(ROOM_SECRET) == ROOM_ID
    assert_durable_workspaces(
        connections, original_workspace, timer_operation, "timer"
    )


@pytest.mark.parametrize("begin", ["BEGIN", "BEGIN IMMEDIATE"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_nested_creation_rejected_without_finishing_caller_transaction(
    connections, monkeypatch, begin, finish
):
    creator, writer, secrets = connections
    original_workspace = writer._workspace_storage.capture()
    creator.connection.execute(begin)
    creator._set_meta("callerOwned", "uncommitted")

    def unexpected_projection():
        pytest.fail("Nested room creation must fail before reading genesis")

    monkeypatch.setattr(
        creator._replication_storage._projection,
        "projected_local_genesis", unexpected_projection,
    )
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        creator.create_iroh_room(ROOM_SECRET)
    assert creator.connection.in_transaction
    assert creator.get_meta("callerOwned") == "uncommitted"
    assert writer.get_meta("callerOwned") is None
    assert writer._workspace_storage.capture() == original_workspace
    assert writer.iroh_room(ROOM_ID) is None
    assert secrets.values == {}
    getattr(creator.connection, finish)()
    expected = "uncommitted" if finish == "commit" else None
    assert writer.get_meta("callerOwned") == expected
