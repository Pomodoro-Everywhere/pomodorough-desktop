"""R52-D02: failed owned read commits roll back without blocking later edits."""

from __future__ import annotations

import sqlite3

import pytest

from pomodorough.storage import Store
from test_storage_replication_modules import MemorySecretStore


def _open_store(tmp_path):
    secrets = MemorySecretStore()
    store = Store(tmp_path / "state.sqlite3", iroh_secret_store=secrets)
    return store, secrets


def _deny_commit(connection):
    def reject_commit(action, operation, _argument, _database, _trigger):
        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(reject_commit)


def _assert_usable_after_failure(store, baseline):
    assert not store.connection.in_transaction
    assert store.load() == baseline
    store.set_meta("r52d02_probe", "ok")
    assert store.get_meta("r52d02_probe") == "ok"
    assert not store.connection.in_transaction


def test_load_commit_failure_rolls_back(tmp_path):
    store, _secrets = _open_store(tmp_path)
    try:
        baseline = store.load()
        _deny_commit(store.connection)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                store.load()
        finally:
            store.connection.set_authorizer(None)
        _assert_usable_after_failure(store, baseline)
        expected = store.load()
    finally:
        store.close()
    reopened = Store(tmp_path / "state.sqlite3",
                     iroh_secret_store=MemorySecretStore())
    try:
        assert reopened.load() == expected
        assert reopened.get_meta("r52d02_probe") == "ok"
        assert not reopened.connection.in_transaction
    finally:
        reopened.close()


def test_load_with_provisional_commit_failure_rolls_back(tmp_path):
    store, _secrets = _open_store(tmp_path)
    try:
        baseline_state, baseline_ids = store.load_with_provisional_auto_breaks()
        _deny_commit(store.connection)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                store.load_with_provisional_auto_breaks()
        finally:
            store.connection.set_authorizer(None)
        assert not store.connection.in_transaction
        reread_state, reread_ids = store.load_with_provisional_auto_breaks()
        assert reread_state == baseline_state
        assert reread_ids == baseline_ids
        store.set_meta("r52d02_probe", "ok")
        assert store.get_meta("r52d02_probe") == "ok"
        assert not store.connection.in_transaction
        expected = store.load_with_provisional_auto_breaks()
    finally:
        store.close()
    reopened = Store(tmp_path / "state.sqlite3",
                     iroh_secret_store=MemorySecretStore())
    try:
        assert reopened.load_with_provisional_auto_breaks() == expected
        assert not reopened.connection.in_transaction
    finally:
        reopened.close()


def test_owned_read_failure_rolls_back(tmp_path):
    for reader in ("load", "load_with_provisional"):
        store, _secrets = _open_store(tmp_path)
        try:
            baseline = store.load()
            original = store.get_meta

            def fail_once(key, default=None):
                store.get_meta = original
                raise ValueError("injected read failure")

            store.get_meta = fail_once
            try:
                with pytest.raises(ValueError, match="injected read failure"):
                    if reader == "load":
                        store.load()
                    else:
                        store.load_with_provisional_auto_breaks()
            finally:
                store.get_meta = original
            _assert_usable_after_failure(store, baseline)
        finally:
            store.close()
        tmp_path.joinpath("state.sqlite3").unlink()


def test_caller_owned_transaction_undisturbed(tmp_path):
    store, _secrets = _open_store(tmp_path)
    try:
        store.connection.execute("BEGIN")
        store._set_meta("callerOwned", "uncommitted")
        state = store.load()
        assert store.connection.in_transaction
        assert state["settings"] is not None
        assert store.get_meta("callerOwned") == "uncommitted"
        store.connection.rollback()
        assert not store.connection.in_transaction
        assert store.get_meta("callerOwned") is None
    finally:
        store.close()


def test_caller_owned_transaction_survives_read_failure(tmp_path):
    store, _secrets = _open_store(tmp_path)
    try:
        store.connection.execute("BEGIN")
        store._set_meta("callerOwned", "uncommitted")
        original = store.get_meta

        def fail_once(key, default=None):
            store.get_meta = original
            raise ValueError("injected read failure")

        store.get_meta = fail_once
        try:
            with pytest.raises(ValueError, match="injected read failure"):
                store.load()
        finally:
            store.get_meta = original
        assert store.connection.in_transaction
        assert store.get_meta("callerOwned") == "uncommitted"
        store.connection.rollback()
        assert not store.connection.in_transaction
    finally:
        store.close()
