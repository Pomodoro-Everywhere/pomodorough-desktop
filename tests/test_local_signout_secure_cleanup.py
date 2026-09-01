from __future__ import annotations

import json
import os
import subprocess
import traceback
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_network import _FakeRevisionReply
from test_network_account_lifecycle import _run_immediately
from test_network_revocation import PlatformKeyring, token_response
from test_secure_store import linux_secret_store

from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.network_account import SignOutCleanupError
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError

API = "https://signout.example.test"


@pytest.fixture
def signout(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    keyring = PlatformKeyring()
    workers, retries, services = deque(), deque(), []
    monkeypatch.setattr("pomodorough.secure_store.shutil.which", lambda _: "/bin/secret-tool")
    monkeypatch.setattr(PlatformSecretStore, "_run", staticmethod(keyring.run))
    monkeypatch.setattr(QThreadPool.globalInstance(), "start", workers.append)
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: retries.append((delay, callback)))

    def create(api=API):
        tokens = TokenStore("signout-device", PlatformSecretStore(tmp_path), tmp_path / "session.json")
        cloud = CloudService("signout-device", api, token_store=tokens, request=Mock(return_value={}))
        cloud._revocation_restore_timer.stop()
        services.append(cloud)
        return cloud

    with linux_secret_store(tmp_path):
        yield SimpleNamespace(
            create=create, keyring=keyring, workers=workers, retries=retries, app=app,
        )
        for cloud in services:
            cloud.shutdown()


def sign_in(cloud, session="captured"):
    cloud._accept_login_tokens(token_response(session))
    cloud.authenticated = True


def fail_secure_delete(signout):
    signout.keyring.reject = lambda command, _input: command[1] == "clear"


def pending(cloud):
    return cloud.token_store.revocations.load_all(cloud.api_base)


def assert_signed_out(cloud):
    assert not cloud.authenticated
    assert cloud.access_token is None
    assert cloud.refresh_token is None
    assert cloud.access_expires_at == datetime.min.replace(tzinfo=UTC)
    assert not cloud.busy
    assert not cloud.deleting_account
    assert cloud._sync_queued is None
    assert cloud.token_store.load() is None


@contextmanager
def failing_tombstone_step(tokens, boundary):
    clear, fdopen = tokens.clear, os.fdopen

    @contextmanager
    def failing_file(*args, **kwargs):
        with fdopen(*args, **kwargs) as fallback:
            with patch.object(fallback, boundary, side_effect=OSError("disk failure")):
                yield fallback

    def failing_clear():
        target = {
            "mkdir": "Path.mkdir", "mkstemp": "tempfile.mkstemp",
            "write": "os.fdopen", "flush": "os.fdopen",
            "replace": "_replace_file_for_durable_commit",
        }.get(boundary, f"os.{boundary}")
        failure = failing_file if boundary in {"write", "flush"} else OSError("disk failure")
        with patch(f"pomodorough.network.{target}", side_effect=failure):
            clear()

    with patch.object(tokens, "clear", side_effect=failing_clear):
        yield


def test_secure_delete_failure_invalidates_memory_stream_ui_and_protected_requests(signout):
    cloud = signout.create()
    sign_in(cloud)
    cloud.busy = cloud.deleting_account = True
    cloud._sync_queued = {"operations": []}
    reply = _FakeRevisionReply()
    cloud._revisions.state.reply = reply
    cloud._revisions.reconnect_timer.start(10_000)
    published, failures = [], []
    cloud.signed_out.connect(lambda: published.append((cloud.authenticated, reply.aborted)))
    cloud.failure.connect(failures.append)
    generation = cloud._account_generation
    fail_secure_delete(signout)

    with pytest.raises(SignOutCleanupError):
        cloud.logout()

    assert_signed_out(cloud)
    assert cloud._account_generation == generation + 1
    assert published == [(False, True)]
    assert reply.deleted and cloud._revisions.state.reply is None
    assert not cloud._revisions.reconnect_timer.isActive()
    assert len(failures) == 1 and "will be retried" in failures[0]
    assert json.loads(cloud.token_store.fallback_path.read_text()) == {"signedOut": True}
    assert cloud.token_store.secret_key in signout.keyring.values
    assert len(pending(cloud)[API]) == 1
    assert len(signout.workers) == len(signout.retries) == 1
    with pytest.raises(ApiError, match="Sign in"):
        cloud._authorized_request("GET", "/api/v1/protected")
    cloud._request.assert_not_called()


@pytest.mark.parametrize("boundary", ["mkdir", "mkstemp", "fchmod", "fdopen", "write", "flush", "fsync", "replace"])
def test_tombstone_precommit_failure_retains_session_and_obligation_then_retries(signout, boundary):
    if boundary == "fchmod" and not hasattr(os, "fchmod"):
        pytest.skip("Descriptor chmod is unavailable")
    cloud = signout.create()
    sign_in(cloud)
    generation = cloud._account_generation
    signed_out = Mock()
    cloud.signed_out.connect(signed_out)
    with (
        failing_tombstone_step(cloud.token_store, boundary),
        pytest.raises(SecureStoreError, match="Session retained"),
    ):
        cloud.logout()

    assert cloud._account_generation == generation
    assert cloud.authenticated and cloud.access_token == "captured-access"
    assert cloud.token_store.load()["refreshToken"] == "captured-refresh"
    assert len(pending(cloud)[API]) == 1
    assert not signout.retries and not signout.workers
    signed_out.assert_not_called()
    cloud.logout()
    assert_signed_out(cloud)
    assert len(pending(cloud)[API]) == 2


@pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="Directory fsync is unavailable")
def test_post_replace_directory_sync_failure_still_invalidates_and_retries(signout):
    cloud = signout.create()
    sign_in(cloud)
    with (
        patch("pomodorough.network.os.fsync", side_effect=[None, OSError("directory sync")]),
        pytest.raises(SignOutCleanupError),
    ):
        cloud.logout()
    assert_signed_out(cloud)
    assert len(pending(cloud)[API]) == 1
    signout.retries.popleft()[1]()
    assert cloud.token_store.secret_key not in signout.keyring.values
    assert not signout.retries


@pytest.mark.parametrize("boundary", ["directory-open", "temporary-unlink"])
def test_post_replace_finalization_failure_preserves_tombstone_and_invalidates(signout, boundary):
    if boundary == "directory-open" and not hasattr(os, "O_DIRECTORY"):
        pytest.skip("Directory fsync is unavailable")
    cloud = signout.create()
    sign_in(cloud)
    open_file = os.open

    def fail_directory(path, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            raise OSError("directory open")
        return open_file(path, flags, *args, **kwargs)

    target = "os.open" if boundary == "directory-open" else "Path.unlink"
    failure = fail_directory if boundary == "directory-open" else OSError("temporary cleanup")
    with patch(f"pomodorough.network.{target}", side_effect=failure), pytest.raises(SignOutCleanupError):
        cloud.logout()
    assert_signed_out(cloud)
    assert len(pending(cloud)[API]) == 1
    signout.retries.popleft()[1]()
    assert cloud.token_store.secret_key not in signout.keyring.values


@pytest.mark.parametrize("boundary", ["origins", "queue"])
def test_enqueue_failure_preserves_all_local_state_and_previous_obligations(signout, boundary):
    cloud = signout.create()
    sign_in(cloud, "previous")
    cloud.logout()
    sign_in(cloud, "current")
    cloud.busy = cloud.deleting_account = True
    cloud._sync_queued = {"pending": True}
    before = pending(cloud)
    generation = cloud._account_generation
    prefix = "oauth-revocation-origins" if boundary == "origins" else "oauth-revocations"
    if boundary == "origins":
        other = signout.create("https://other-signout.example.test")
        sign_in(other, "other")
        cloud = other
        generation = cloud._account_generation
    signout.keyring.reject = lambda command, _input: command[1] == "store" and command[-1].startswith(prefix)
    with patch.object(cloud.token_store, "clear") as clear, pytest.raises(SecureStoreError):
        cloud.logout()
    clear.assert_not_called()
    assert cloud.authenticated and cloud._account_generation == generation
    assert cloud.token_store.load()["refreshToken"] == cloud.refresh_token
    assert pending(cloud)[API] == before[API]
    if boundary == "queue":
        assert cloud.busy and cloud.deleting_account and cloud._sync_queued == {"pending": True}


def test_tombstone_failure_cannot_hide_credentials_bound_to_another_origin(signout):
    original = signout.create()
    sign_in(original)
    original.shutdown()
    replacement = signout.create("https://replacement.example.test")
    with (
        patch.object(replacement.token_store, "_write_fallback", side_effect=OSError("disk")),
        pytest.raises(SecureStoreError, match="Session retained"),
    ):
        replacement.logout()
    assert replacement._account_generation == 0
    assert len(pending(replacement)[API]) == 1
    assert replacement.token_store.load_for_revocation()["refreshToken"] == "captured-refresh"
    assert not signout.retries


def test_cleanup_retry_keeps_tombstone_and_queue_without_republishing(signout):
    cloud = signout.create()
    sign_in(cloud)
    published = Mock()
    cloud.signed_out.connect(published)
    fail_secure_delete(signout)
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    before = pending(cloud)
    signout.keyring.reject = lambda _command, _input: False
    delay, retry = signout.retries.popleft()
    assert delay == 1000
    retry()
    assert not signout.retries
    assert cloud.token_store.secret_key not in signout.keyring.values
    assert pending(cloud) == before
    assert_signed_out(cloud)
    published.assert_called_once_with()


def test_cleanup_retry_backoff_caps_without_abandoning_cleanup(signout):
    cloud = signout.create()
    sign_in(cloud)
    fail_secure_delete(signout)
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    delays = []
    for _attempt in range(10):
        delay, retry = signout.retries.popleft()
        delays.append(delay)
        retry()
    assert delays == [1000, 2000, 4000, 8000, 16000] + [30000] * 5
    assert len(signout.retries) == 1
    assert len(pending(cloud)[API]) == 1
    assert_signed_out(cloud)


@pytest.mark.parametrize("boundary", ["read", "tombstone"])
def test_cleanup_retry_storage_failure_remains_explicit_and_recoverable(signout, boundary):
    cloud = signout.create()
    sign_in(cloud)
    fail_secure_delete(signout)
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    failures = []
    cloud.failure.connect(failures.append)
    method = "load_for_revocation" if boundary == "read" else "_write_fallback"
    with patch.object(cloud.token_store, method, side_effect=OSError("storage")):
        signout.retries.popleft()[1]()
    assert len(failures) == len(signout.retries) == 1
    assert "will be retried" in failures[0]
    assert_signed_out(cloud)
    signout.keyring.reject = lambda _command, _input: False
    signout.retries.popleft()[1]()
    assert cloud.token_store.secret_key not in signout.keyring.values
    assert len(pending(cloud)[API]) == 1
    assert not signout.retries


@pytest.mark.parametrize("transition", ["login", "shutdown", "repeat-logout", "stored-login", "other-origin"])
def test_stale_cleanup_retry_cannot_delete_a_later_session(signout, transition):
    cloud = signout.create()
    sign_in(cloud)
    fail_secure_delete(signout)
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    retry = signout.retries.popleft()[1]
    signout.keyring.reject = lambda _command, _input: False
    if transition == "login":
        sign_in(cloud, "current")
    elif transition == "shutdown":
        cloud.shutdown()
    elif transition == "repeat-logout":
        cloud.logout()
    elif transition == "stored-login":
        cloud.token_store.save(token_response("current"))
    else:
        sign_in(signout.create("https://other-signout.example.test"), "current")
    before = dict(signout.keyring.values)
    with patch.object(cloud.token_store, "clear") as clear:
        retry()
    clear.assert_not_called()
    assert signout.keyring.values == before
    assert not signout.retries


def test_restart_cannot_restore_stale_credentials_and_revocation_still_replays(signout):
    cloud = signout.create()
    sign_in(cloud)
    fail_secure_delete(signout)
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    cloud.shutdown()
    signout.workers.clear()
    restarted = signout.create()
    with patch.object(restarted, "_start", side_effect=_run_immediately):
        restarted.restore()
    assert_signed_out(restarted)
    restarted._request.assert_not_called()
    restarted._restore_revocations()
    signout.workers.popleft().run()
    signout.workers.popleft().run()
    restarted._request.assert_called_once_with(
        "POST", API + "/api/v1/auth/logout", {}, access_token="captured-access",
    )
    assert pending(restarted)[API] == {}
    signout.keyring.reject = lambda _command, _input: False
    restarted.logout()
    assert restarted.token_store.secret_key not in signout.keyring.values


def test_cleanup_failure_keeps_each_account_revocation_independent(signout):
    cloud = signout.create()
    for account in ("first", "second"):
        sign_in(cloud, account)
        fail_secure_delete(signout)
        with pytest.raises(SignOutCleanupError):
            cloud.logout()
    sign_in(cloud, "current")
    for _delay, retry in signout.retries:
        retry()
    assert len(pending(cloud)[API]) == 2
    while signout.workers:
        signout.workers.popleft().run()
    assert {call.kwargs["access_token"] for call in cloud._request.call_args_list} == {
        "first-access", "second-access",
    }
    assert pending(cloud)[API] == {}
    assert cloud.token_store.load()["refreshToken"] == "current-refresh"
    assert cloud.access_token == "current-access" and cloud.authenticated


def test_stale_refresh_and_worker_callbacks_cannot_reinstall_signed_out_session(signout):
    cloud = signout.create()
    sign_in(cloud)
    cloud.access_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    old_result, old_error = Mock(), Mock()
    cloud._start(lambda: {}, old_result, old_error)
    old_worker = signout.workers.popleft()
    fail_secure_delete(signout)

    def refresh_then_logout(*_args, **_kwargs):
        with pytest.raises(SignOutCleanupError):
            cloud.logout()
        return token_response("stale")

    cloud._request.side_effect = refresh_then_logout
    with pytest.raises(ApiError, match="cancelled"):
        cloud._ensure_access()
    old_worker.signals.result.emit({"old": True})
    old_worker.signals.error.emit(ApiError("old"))
    old_worker.signals.finished.emit()
    old_result.assert_not_called()
    old_error.assert_not_called()
    assert_signed_out(cloud)


@pytest.mark.parametrize("boundary", ["enqueue", "delete", "retry"])
def test_storage_error_details_never_reach_failure_signal_or_traceback(signout, boundary):
    cloud = signout.create()
    sign_in(cloud)
    failures = []
    cloud.failure.connect(failures.append)
    secret_error = subprocess.SubprocessError("captured-access captured-refresh")
    target = cloud._accounts if boundary == "enqueue" else cloud.token_store.secret_store
    method = "enqueue_revocation" if boundary == "enqueue" else "delete"
    with patch.object(target, method, side_effect=secret_error), pytest.raises(SecureStoreError) as raised:
        cloud.logout()
    if boundary == "retry":
        with patch.object(cloud.token_store.secret_store, "delete", side_effect=secret_error):
            signout.retries.popleft()[1]()
    visible = repr(failures) + "".join(traceback.format_exception(raised.value))
    assert failures
    assert "captured-access" not in visible and "captured-refresh" not in visible


def test_unreadable_store_after_failed_tombstone_is_explicit_and_preserves_session(signout):
    cloud = signout.create()
    sign_in(cloud)
    with (
        patch.object(cloud.token_store, "clear", side_effect=OSError("write")),
        patch.object(cloud.token_store, "load_for_revocation", side_effect=OSError("read")),
        pytest.raises(SecureStoreError, match="Session retained"),
    ):
        cloud.logout()
    assert cloud.authenticated and cloud.access_token == "captured-access"
    assert len(pending(cloud)[API]) == 1
    assert not signout.retries
