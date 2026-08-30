from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_signout_cleanup_recovery import FileVault, fixture_environment, response

from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.network_account import SignOutCleanupError
from pomodorough.secure_store import SecureStoreError

API = "https://signout-commit.example.test"
POSIX_PERMISSIONS = os.name != "nt" and os.geteuid() != 0
DAMAGE = [
    "readable",
    pytest.param("unreadable", marks=pytest.mark.skipif(
        not POSIX_PERMISSIONS, reason="Requires enforced POSIX file permissions",
    )),
    "removed",
    "corrupt",
]


@pytest.fixture
def signout_commit(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    workers, retries, published, failures = [], [], [], []
    monkeypatch.setattr(QThreadPool.globalInstance(), "start", workers.append)
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: retries.append((delay, callback)))
    with fixture_environment(tmp_path):
        tokens = TokenStore("commit-device", FileVault(tmp_path), tmp_path / "session.json")
        cloud = CloudService("commit-device", API, token_store=tokens, request=Mock(return_value={}))
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud._accept_login_tokens(response())
        cloud.authenticated = True
        cloud.signed_out.connect(lambda: published.append(cloud.authenticated))
        cloud.failure.connect(failures.append)
        yield SimpleNamespace(
            cloud=cloud, app=app, workers=workers, retries=retries,
            published=published, failures=failures, root=tmp_path,
        )
        tmp_path.chmod(0o700)
        if tokens.fallback_path.is_file():
            tokens.fallback_path.chmod(0o600)
        cloud.shutdown()


def damage_committed_tombstone(path, damage):
    assert path.read_text() == '{"signedOut":true}'
    if damage == "unreadable":
        path.chmod(0)
        with pytest.raises(PermissionError):
            path.read_bytes()
    elif damage == "removed":
        path.unlink()
        assert not path.exists()
    elif damage == "corrupt":
        path.write_bytes(b'{"signedOut":')


@contextmanager
def failing_after_replace(tokens, boundary, damage):
    sync, unlink = os.fsync, Path.unlink

    def delete(_key):
        damage_committed_tombstone(tokens.fallback_path, damage)
        raise SecureStoreError("old-synthetic-refresh secure deletion failed")

    def directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            damage_committed_tombstone(tokens.fallback_path, damage)
            raise OSError("old-synthetic-refresh directory sync failed")
        return sync(descriptor)

    def temporary_unlink(path, *args, **kwargs):
        if path.parent == tokens.fallback_path.parent and path.name.startswith(".session.json."):
            damage_committed_tombstone(tokens.fallback_path, damage)
            raise OSError("old-synthetic-refresh temporary cleanup failed")
        return unlink(path, *args, **kwargs)

    if boundary == "secure-delete":
        with patch.object(tokens.secret_store, "delete", side_effect=delete):
            yield
    elif boundary == "directory-sync":
        with patch("pomodorough.network.os.fsync", side_effect=directory_sync):
            yield
    else:
        with patch.object(Path, "unlink", temporary_unlink):
            yield


def assert_local_invalidation(case, generation):
    cloud = case.cloud
    assert not cloud.authenticated and cloud.access_token is None
    assert cloud.refresh_token is None
    assert cloud.access_expires_at == datetime.min.replace(tzinfo=UTC)
    assert cloud._account_generation == generation + 1
    assert not cloud.busy and not cloud.deleting_account and cloud._sync_queued is None
    assert case.published == [False]
    assert len(case.failures) == 1 and "will be retried" in case.failures[0]
    assert "old-synthetic" not in case.failures[0]
    assert len(case.workers) == len(case.retries) == 1
    assert case.retries[0][0] == 1000
    pending = cloud.token_store.revocations.load(API)
    assert len(pending) == 1
    assert next(iter(pending.values()))["refreshToken"] == response()["refreshToken"]
    with pytest.raises(ApiError):
        cloud._session.ensure_access(generation)
    cloud._request.assert_not_called()


@pytest.mark.parametrize("damage", DAMAGE)
@pytest.mark.parametrize("boundary", [
    "secure-delete",
    pytest.param("directory-sync", marks=pytest.mark.skipif(
        not hasattr(os, "O_DIRECTORY"), reason="Directory fsync is unavailable",
    )),
    "temporary-unlink",
])
def test_post_replace_failure_invalidates_without_rereading_tombstone(signout_commit, boundary, damage):
    cloud = signout_commit.cloud
    cloud.busy = cloud.deleting_account = True
    cloud._sync_queued = {"operations": []}
    generation = cloud._account_generation
    with failing_after_replace(cloud.token_store, boundary, damage), pytest.raises(SignOutCleanupError):
        cloud.logout()
    assert_local_invalidation(signout_commit, generation)
    assert cloud.token_store.secret_store.load(cloud.token_store.secret_key) is not None
    if damage != "removed":
        with pytest.raises((ApiError, SecureStoreError, PermissionError)):
            cloud._authorized_request("GET", "/api/v1/protected")
        cloud._request.assert_not_called()


@contextmanager
def unwritable_tombstone_directory(root):
    create = tempfile.mkstemp

    def denied_create(*args, **kwargs):
        root.chmod(0o500)
        try:
            return create(*args, **kwargs)
        finally:
            root.chmod(0o700)

    with patch("pomodorough.network.tempfile.mkstemp", side_effect=denied_create):
        yield


def assert_session_retained(case, generation, account="old"):
    cloud = case.cloud
    assert cloud.authenticated and cloud.access_token == response(account)["accessToken"]
    assert cloud.refresh_token == response(account)["refreshToken"]
    assert cloud._account_generation == generation
    assert not case.published and not case.retries
    assert case.failures == ["Sign out could not be persisted. Session retained; retry sign out."]
    assert cloud._authorized_request("GET", "/api/v1/protected") == {}
    assert cloud._request.call_args.kwargs["access_token"] == response(account)["accessToken"]


@pytest.mark.parametrize("boundary", [
    "directory-target",
    pytest.param("read-only-parent", marks=pytest.mark.skipif(
        not POSIX_PERMISSIONS, reason="Requires enforced POSIX directory permissions",
    )),
])
def test_pre_replace_filesystem_failure_retains_session(signout_commit, boundary):
    cloud = signout_commit.cloud
    generation = cloud._account_generation
    if boundary == "directory-target":
        cloud.token_store.fallback_path.mkdir()
        with pytest.raises(SecureStoreError, match="Session retained"):
            cloud.logout()
        assert cloud.token_store.fallback_path.is_dir()
    else:
        with unwritable_tombstone_directory(signout_commit.root), pytest.raises(SecureStoreError):
            cloud.logout()
        assert not cloud.token_store.fallback_path.exists()
    assert_session_retained(signout_commit, generation)
    assert len(cloud.token_store.revocations.load(API)) == 1
    assert cloud.token_store.secret_store.load(cloud.token_store.secret_key) is not None
    assert not signout_commit.workers


@pytest.mark.skipif(not POSIX_PERMISSIONS, reason="Requires enforced POSIX directory permissions")
def test_committed_previous_logout_cannot_classify_new_precommit_failure(signout_commit):
    cloud = signout_commit.cloud
    cloud.logout()
    cloud._accept_login_tokens(response("replacement"))
    cloud.authenticated = True
    signout_commit.published.clear()
    generation = cloud._account_generation
    with unwritable_tombstone_directory(signout_commit.root), pytest.raises(SecureStoreError):
        cloud.logout()
    assert_session_retained(signout_commit, generation, "replacement")
    assert cloud.token_store.load()["refreshToken"] == response("replacement")["refreshToken"]
    assert len(cloud.token_store.revocations.load(API)) == 2
