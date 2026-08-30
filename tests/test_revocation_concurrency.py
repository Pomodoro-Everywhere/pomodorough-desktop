from __future__ import annotations

import base64
import json
import multiprocessing
import os
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_network_revocation import PlatformKeyring
from test_secure_store import MemorySecretStore, linux_secret_store
from test_storage_revocation import credentials

from pomodorough.network import ApiError, TokenStore
from pomodorough.network_account import AccountLifecycle
from pomodorough.network_session import SessionState
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError
from pomodorough.storage_revocation import PendingSessionRevocations

API = "https://origin.example.test"


@pytest.fixture
def native_vault(tmp_path, monkeypatch):
    vault = PlatformKeyring()
    monkeypatch.setattr("pomodorough.secure_store.shutil.which", lambda _: "/bin/secret-tool")
    monkeypatch.setattr(PlatformSecretStore, "_run", staticmethod(vault.run))
    with linux_secret_store(tmp_path):
        yield vault


@pytest.mark.parametrize("overlap", [False, True], ids=["serialized-control", "racing-instances"])
def test_acknowledgment_cannot_erase_newer_obligation(tmp_path, native_vault, monkeypatch, overlap):
    old = PendingSessionRevocations(PlatformSecretStore(tmp_path / "gui"), "device")
    new = PendingSessionRevocations(PlatformSecretStore(tmp_path / "cli"), "device")
    old.save(API, "old", credentials("old"))
    ready, release, saving = threading.Event(), threading.Event(), threading.Event()
    original = PlatformSecretStore._run

    def delayed(command, *, input_text=None):
        if overlap and command[1] == "store" and json.loads(base64.b64decode(input_text)).get("pending") == {}:
            ready.set()
            assert release.wait(3)
        return original(command, input_text=input_text)

    def save_new():
        saving.set()
        new.save(API, "new", credentials("new"))

    monkeypatch.setattr(PlatformSecretStore, "_run", staticmethod(delayed))
    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup = executor.submit(old.acknowledge, API, "old")
        try:
            if overlap:
                assert ready.wait(3)
            else:
                cleanup.result(timeout=3)
            writer = executor.submit(save_new)
            assert saving.wait(3)
            if overlap:
                assert not writer.done()
        finally:
            release.set()
        cleanup.result(timeout=3)
        writer.result(timeout=3)
    assert new.load(API) == {"new": credentials("new")}


def test_multiple_memory_adapters_share_read_modify_write_serialization():
    secrets = MemorySecretStore({})
    adapters = [PendingSessionRevocations(secrets, "device") for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(adapter.save, API, str(index), credentials(str(index))) for index, adapter in enumerate(adapters)]
        for future in futures:
            future.result(timeout=3)
    assert len(adapters[0].load(API)) == 12


def test_concurrent_new_origins_remain_discoverable(tmp_path, native_vault):
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(PendingSessionRevocations(PlatformSecretStore(tmp_path), "device").save,
                                   f"https://origin-{index}.test", "same-id", credentials(str(index))) for index in range(16)]
        for future in futures:
            future.result(timeout=3)
    pending = PendingSessionRevocations(PlatformSecretStore(tmp_path), "device").load_all(API)
    assert sum(len(queue) for queue in pending.values()) == 16


def test_failed_index_write_never_creates_unindexed_obligation(tmp_path, native_vault):
    pending = PendingSessionRevocations(PlatformSecretStore(tmp_path), "device")
    native_vault.reject = lambda command, _input: command[1] == "store" and "origins-v1:" in command[-1]
    with pytest.raises(SecureStoreError):
        pending.save(API, "old", credentials("old"))
    assert pending.load(API) == {}


@pytest.mark.parametrize("encoded", [b"null", b"[]", b"not-json", b'{"version":true,"origins":[]}', b'{"version":1,"origins":[null]}'])
def test_malformed_origin_index_never_overwrites_existing_secrets(encoded):
    secrets = MemorySecretStore({})
    pending = PendingSessionRevocations(secrets, "device")
    pending.save(API, "old", credentials("old"))
    secrets.values[pending._origins_key] = encoded
    before = dict(secrets.values)
    with pytest.raises(SecureStoreError):
        pending.load_all(API)
    with pytest.raises(SecureStoreError):
        pending.save(API, "new", credentials("new"))
    assert secrets.values == before


def _process_vault(root, ready, release):
    def run(command, *, input_text=None):
        key = command[-1]
        if command[1] == "store" and json.loads(base64.b64decode(input_text)).get("pending") == {}:
            ready.set()
            assert release.wait(10)
        with sqlite3.connect(root / "test-vault.sqlite") as connection:
            if command[1] == "store":
                connection.execute("INSERT OR REPLACE INTO secrets VALUES (?, ?)", (key, input_text))
            row = connection.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
        return subprocess.CompletedProcess(command, 0 if row else 1, row[0] if row else "", "")
    return run


def _process_mutation(root_text, operation, ready, release, completed):
    root = Path(root_text)
    with (
        linux_secret_store(root),
        patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
        patch.object(PlatformSecretStore, "_run", staticmethod(_process_vault(root, ready, release))),
    ):
        pending = PendingSessionRevocations(PlatformSecretStore(root / operation), "device")
        if operation == "acknowledge":
            pending.acknowledge(API, "old")
        else:
            ready.set()
            pending.save(API, "new", credentials("new"))
        completed.set()


def test_gui_and_cli_processes_cannot_lose_obligation(tmp_path, native_vault):
    pending = PendingSessionRevocations(PlatformSecretStore(tmp_path), "device")
    pending.save(API, "old", credentials("old"))
    with sqlite3.connect(tmp_path / "test-vault.sqlite") as connection:
        connection.execute("CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany("INSERT INTO secrets VALUES (?, ?)", native_vault.values.items())
    context = multiprocessing.get_context("spawn")
    ready, release, saving = context.Event(), context.Event(), context.Event()
    cleaned, saved = context.Event(), context.Event()
    cleanup = context.Process(target=_process_mutation, args=(str(tmp_path), "acknowledge", ready, release, cleaned))
    writer = context.Process(target=_process_mutation, args=(str(tmp_path), "save", saving, release, saved))
    cleanup.start()
    try:
        assert ready.wait(10)
        writer.start()
        assert saving.wait(10)
        assert not saved.wait(0.2)
    finally:
        release.set()
        for process in (cleanup, writer):
            if process.pid is not None:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(3)
    assert cleanup.exitcode == writer.exitcode == 0
    assert cleaned.is_set() and saved.is_set()
    with patch.object(PlatformSecretStore, "_run", staticmethod(_process_vault(tmp_path, ready, release))):
        assert pending.load(API) == {"new": credentials("new")}


def test_platform_lock_releases_after_exception(tmp_path, native_vault):
    with pytest.raises(RuntimeError), PlatformSecretStore(tmp_path).lock("test-key"):
        raise RuntimeError("injected storage failure")
    with PlatformSecretStore(tmp_path).lock("test-key"):
        pass
    for path in tmp_path.rglob("*.lock"):
        assert path.read_bytes() == (b"\0" if os.name == "nt" else b"")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_lock_scope_matches_vault_namespace_not_adapter_root(tmp_path, native_vault, monkeypatch, platform):
    monkeypatch.setattr("pomodorough.secure_store.sys_platform", lambda: platform)
    first = PlatformSecretStore(tmp_path / "gui")
    second = PlatformSecretStore(tmp_path / "cli")
    separate = PlatformSecretStore(tmp_path, service="other", kind="other")
    assert first._lock_path("device") == second._lock_path("device")
    assert first._lock_path("device") != separate._lock_path("device")


def test_windows_lock_uses_only_nonsecret_byte_and_unlocks(tmp_path):
    store = PlatformSecretStore(tmp_path)
    locking = Mock(LK_LOCK=1, LK_UNLCK=2)
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    with patch("pomodorough.secure_store.os", windows_os), patch.dict("sys.modules", msvcrt=locking):
        with store.lock("device"):
            assert store._lock_path("device").read_bytes() == b"\0"
        with store.lock("device"):
            pass
    assert [call.args[1:] for call in locking.locking.call_args_list] == [(1, 1), (2, 1), (1, 1), (2, 1)]


def lifecycle(root, request):
    tokens = TokenStore("device", PlatformSecretStore(root), root / "session.json")
    return AccountLifecycle(API, SessionState(), tokens, request, lambda text: text,
                            lambda: datetime.now(UTC), tokens.revocations)


def test_same_job_threads_claim_entire_rotation_and_logout(tmp_path, native_vault):
    first_at_logout, release, second_started = threading.Event(), threading.Event(), threading.Event()
    pending = PendingSessionRevocations(PlatformSecretStore(tmp_path), "device")
    pending.save(API, "same", credentials("old") | {"accessTokenIsFresh": False})

    def rotating_request(_method, url, body, **kwargs):
        if url.endswith("/refresh"):
            assert body == {"refreshToken": "old-refresh"}
            return credentials("rotated")
        assert kwargs["access_token"] == "rotated-access"
        first_at_logout.set()
        assert release.wait(3)
        raise ApiError("offline", 503)

    first = lifecycle(tmp_path / "gui", rotating_request)
    second_request = Mock(side_effect=ApiError("offline", 503))
    second = lifecycle(tmp_path / "cli", second_request)
    stale = second.pending_revocations()[0]

    def replay():
        second_started.set()
        second.revoke(stale)

    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(first.revoke, first.pending_revocations()[0])
        try:
            assert first_at_logout.wait(3)
            replay_worker = executor.submit(replay)
            assert second_started.wait(3)
            pending.save(API, "other", credentials("other"))
            assert not replay_worker.done()
            second_request.assert_not_called()
        finally:
            release.set()
        for result in (worker, replay_worker):
            with pytest.raises(ApiError):
                result.result(timeout=3)
    assert pending.load(API)["same"] == credentials("rotated")
    second_request.assert_called_once_with("POST", API + "/api/v1/auth/logout", {}, access_token="rotated-access")
    assert pending.load(API)["other"] == credentials("other")


def test_stale_instance_cannot_resurrect_acknowledged_job(tmp_path, native_vault):
    first = lifecycle(tmp_path / "gui", Mock(return_value={}))
    first.revocations.save(API, "same", credentials("old"))
    second_request = Mock(side_effect=ApiError("already revoked", 401))
    second = lifecycle(tmp_path / "cli", second_request)
    stale = second.pending_revocations()[0]
    first.revoke(first.pending_revocations()[0])
    second.revoke(stale)
    assert second.revocations.load(API) == {}
    assert stale.acknowledged
    second_request.assert_not_called()


def test_failed_local_rotation_cannot_replace_newer_durable_rotation(tmp_path, native_vault):
    first = lifecycle(tmp_path, Mock(return_value=credentials("first")))
    first.revocations.save(API, "same", credentials("old") | {"accessTokenIsFresh": False})
    captured = first.pending_revocations()[0]
    native_vault.reject = lambda command, value: command[1] == "store" and value is not None and b"first-access" in base64.b64decode(value)
    with pytest.raises(SecureStoreError):
        first.revoke(captured)
    native_vault.reject = lambda _command, _value: False
    second = lifecycle(tmp_path, Mock(side_effect=[credentials("second"), ApiError("offline", 503)]))
    with pytest.raises(ApiError):
        second.revoke(second.pending_revocations()[0])
    first.request = Mock(side_effect=ApiError("offline", 503))
    with pytest.raises(ApiError):
        first.revoke(captured)
    first.request.assert_called_once_with("POST", API + "/api/v1/auth/logout", {}, access_token="second-access")
    assert first.revocations.load(API)["same"] == credentials("second")


def test_replay_of_missing_bound_job_does_not_enqueue(tmp_path, native_vault):
    account = lifecycle(tmp_path, Mock())
    missing = account.revocation("access", "refresh", True, identifier="missing")
    account.revoke(missing)
    assert account.revocations.load(API) == {}
    account.request.assert_not_called()


def test_explicit_enqueue_cannot_replace_existing_rotation_or_ack(tmp_path, native_vault):
    account = lifecycle(tmp_path, Mock())
    for acknowledged in (False, True):
        current = credentials("rotated") | {"acknowledged": acknowledged}
        account.revocations.save(API, "same", current)
        stale = account.revocation("old-access", "old-refresh", False, identifier="same")
        account.enqueue_revocation(stale)
        assert account.revocations.load(API)["same"] == current


def _process_replay(root_text, role, outcome, restored, start, at_logout, release, completed):
    root = Path(root_text)

    def request(_method, url, body, **kwargs):
        with sqlite3.connect(root / "test-vault.sqlite") as connection:
            connection.execute("INSERT INTO requests VALUES (?, ?, ?)",
                               (role, url, kwargs.get("access_token") or body["refreshToken"]))
        if url.endswith("/refresh"):
            return credentials("rotated")
        if role == "first":
            at_logout.set()
            if outcome == "crash":
                while True:
                    time.sleep(1)
            assert release.wait(10)
        if outcome != "acknowledged":
            raise ApiError("offline", 503)
        return {}

    with (
        linux_secret_store(root),
        patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
        patch.object(PlatformSecretStore, "_run", staticmethod(_process_vault(root, at_logout, release))),
    ):
        account = lifecycle(root / role, request)
        snapshot = account.pending_revocations()[0]
        restored.set()
        assert start.wait(10)
        try:
            account.revoke(snapshot)
        except ApiError:
            assert outcome != "acknowledged"
        completed.set()


@pytest.mark.parametrize("outcome", ["acknowledged", "offline", "crash"])
def test_same_job_spawned_processes_revalidate_after_claim(tmp_path, native_vault, outcome):
    pending = PendingSessionRevocations(PlatformSecretStore(tmp_path), "device")
    pending.save(API, "same", credentials("old") | {"accessTokenIsFresh": False})
    with sqlite3.connect(tmp_path / "test-vault.sqlite") as connection:
        connection.execute("CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE requests (role TEXT, url TEXT, token TEXT)")
        connection.executemany("INSERT INTO secrets VALUES (?, ?)", native_vault.values.items())
    context = multiprocessing.get_context("spawn")
    at_logout, release = context.Event(), context.Event()
    restored = [context.Event(), context.Event()]
    starts = [context.Event(), context.Event()]
    completed = [context.Event(), context.Event()]
    processes = [context.Process(target=_process_replay, args=(str(tmp_path), role, outcome,
                 restored[index], starts[index], at_logout, release, completed[index]))
                 for index, role in enumerate(("first", "second"))]
    for process in processes:
        process.start()
    try:
        assert all(event.wait(10) for event in restored)
        starts[0].set()
        assert at_logout.wait(10)
        starts[1].set()
        assert not completed[1].wait(0.2)
        if outcome == "crash":
            processes[0].terminate()
            processes[0].join(3)
    finally:
        release.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(3)
    assert processes[1].exitcode == 0
    assert processes[0].exitcode != 0 if outcome == "crash" else processes[0].exitcode == 0
    with patch.object(PlatformSecretStore, "_run", staticmethod(_process_vault(tmp_path, at_logout, release))):
        assert pending.load(API) == ({} if outcome == "acknowledged" else {"same": credentials("rotated")})
    with sqlite3.connect(tmp_path / "test-vault.sqlite") as connection:
        calls = connection.execute("SELECT role, url, token FROM requests").fetchall()
    assert calls[:2] == [("first", API + "/api/v1/auth/refresh", "old-refresh"),
                         ("first", API + "/api/v1/auth/logout", "rotated-access")]
    assert calls[2:] == ([] if outcome == "acknowledged" else [("second", API + "/api/v1/auth/logout", "rotated-access")])
