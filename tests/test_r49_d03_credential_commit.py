from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from pomodorough.network import TokenStore
from pomodorough.network_session import AuthenticatedSession, SessionState
from pomodorough.secure_store import TokenCommitPendingError


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _tokens(name: str) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def _make_session(store: TokenStore) -> AuthenticatedSession:
    state = SessionState()
    return AuthenticatedSession(
        "https://example.test",
        state,
        store,
        lambda *a, **k: {},
        lambda k, **v: k,
        lambda: datetime.now(UTC),
        lambda: 0.0,
        lambda: 0,
    )


def _make_store(tmp: Path, secret: _MemorySecretStore) -> TokenStore:
    store = TokenStore(
        "d03-device", secret_store=secret, fallback_path=tmp / "session.json"
    )
    store.bind_api("https://example.test")
    return store


def _secret_refresh(secret: _MemorySecretStore) -> str | None:
    raw = secret.values.get("oauth:d03-device")
    if raw is None:
        return None
    return json.loads(raw.decode())["refreshToken"]


def _identity_bytes(store: TokenStore) -> bytes | None:
    path = store._account_deletion_identity_path()
    return path.read_bytes() if path.exists() else None


def _commit_exists(store: TokenStore) -> bool:
    return store._credential_commit_path().exists()


def _assert_deletion_pending(store: TokenStore) -> None:
    with store.account_deletion_credentials_locked():
        with pytest.raises(TokenCommitPendingError):
            store._account_deletion_record_locked()


def _assert_deletion_matches(store: TokenStore, refresh: str) -> None:
    with store.account_deletion_credentials_locked():
        identity = store._account_deletion_identity_locked()
        assert identity is not None
        stored = store._account_deletion_candidate_locked()
        assert stored is not None
        assert stored["refreshToken"] == refresh
        assert store._stored_credentials_match_identity(stored, identity)


def test_identity_replace_failure_keeps_secret_and_memory_new() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        old_identity = _identity_bytes(store)
        assert old_identity is not None
        new = _tokens("new")
        real_replace = os.replace
        identity_path = store._account_deletion_identity_path()

        def replace(src: Path, dst: Path, *a: object, **k: object) -> None:
            if Path(dst) == identity_path:
                raise PermissionError("identity replace denied")
            return real_replace(src, dst, *a, **k)

        with patch("pomodorough.network.os.replace", side_effect=replace):
            with pytest.raises(TokenCommitPendingError) as pending:
                session.accept_tokens(new)
            assert pending.value.during_recovery is False
            assert _secret_refresh(secret) == "new-refresh"
            assert session.state.refresh_token == "new-refresh"
            assert session.state.access_token == "new-access"
            assert _identity_bytes(store) == old_identity
            assert _commit_exists(store) is True
            _assert_deletion_pending(store)
        session.accept_tokens(new)
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
        assert _identity_bytes(store) != old_identity
        assert _commit_exists(store) is False
        _assert_deletion_matches(store, "new-refresh")


def test_identity_replace_failure_restart_recovers_without_retry() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        old_identity = _identity_bytes(store)
        new = _tokens("new")
        real_replace = os.replace
        identity_path = store._account_deletion_identity_path()

        def replace(src: Path, dst: Path, *a: object, **k: object) -> None:
            if Path(dst) == identity_path:
                raise PermissionError("identity replace denied")
            return real_replace(src, dst, *a, **k)

        with patch("pomodorough.network.os.replace", side_effect=replace):
            with pytest.raises(TokenCommitPendingError):
                session.accept_tokens(new)
        assert _identity_bytes(store) == old_identity
        restarted = _make_store(tmp, secret)
        assert restarted.load()["refreshToken"] == "new-refresh"
        assert _identity_bytes(restarted) != old_identity
        assert _commit_exists(restarted) is False
        _assert_deletion_matches(restarted, "new-refresh")


def test_identity_tempfile_failure_is_post_commit_pending() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        old_identity = _identity_bytes(store)
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*a: object, **k: object) -> tuple[int, str]:
            prefix = str(k.get("prefix", ""))
            if "account-identity" in prefix:
                raise OSError("identity tempfile denied")
            return real_mkstemp(*a, **k)

        with patch("pomodorough.network.tempfile.mkstemp", side_effect=mkstemp):
            with pytest.raises(TokenCommitPendingError):
                session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
        assert _identity_bytes(store) == old_identity
        assert _commit_exists(store) is True
        session.accept_tokens(_tokens("new"))
        assert _identity_bytes(store) != old_identity
        assert _commit_exists(store) is False


def test_identity_fsync_failure_is_post_commit_pending() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        old_identity = _identity_bytes(store)
        real_mkstemp = tempfile.mkstemp
        identity_fds: set[int] = set()
        real_fsync = os.fsync

        def mkstemp(*a: object, **k: object) -> tuple[int, str]:
            fd, name = real_mkstemp(*a, **k)
            if "account-identity" in str(k.get("prefix", "")):
                identity_fds.add(fd)
            return fd, name

        def fsync(fd: int) -> None:
            if fd in identity_fds:
                raise OSError("identity fsync denied")
            return real_fsync(fd)

        with (
            patch("pomodorough.network.tempfile.mkstemp", side_effect=mkstemp),
            patch("pomodorough.network.os.fsync", side_effect=fsync),
        ):
            with pytest.raises(TokenCommitPendingError):
                session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
        assert _identity_bytes(store) == old_identity
        session.accept_tokens(_tokens("new"))
        assert _commit_exists(store) is False


def test_identity_directory_sync_failure_replays_idempotently() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        identity_path = store._account_deletion_identity_path()
        import pomodorough.network as net

        real_sync = net._sync_replaced_file_directory

        def sync(path: Path, filesystem: object = os) -> None:
            if Path(path) == identity_path:
                raise OSError("identity directory sync denied")
            return real_sync(path, filesystem)

        with patch("pomodorough.network._sync_replaced_file_directory", side_effect=sync):
            with pytest.raises(TokenCommitPendingError):
                session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
        assert _identity_bytes(store) is not None
        assert _commit_exists(store) is True
        session.accept_tokens(_tokens("new"))
        assert _commit_exists(store) is False
        _assert_deletion_matches(store, "new-refresh")


def test_fallback_removal_failure_is_post_commit_pending() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        store.fallback_path.write_text('{"stale":true}')
        old_identity = _identity_bytes(store)
        real_unlink = Path.unlink
        fallback = store.fallback_path

        def unlink(self: Path, *a: object, **k: object) -> None:
            if self == fallback:
                raise PermissionError("fallback removal denied")
            return real_unlink(self, *a, **k)

        with patch("pathlib.Path.unlink", autospec=True, side_effect=unlink):
            with pytest.raises(TokenCommitPendingError):
                session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
        assert _identity_bytes(store) == old_identity
        assert fallback.exists() is True
        session.accept_tokens(_tokens("new"))
        assert fallback.exists() is False
        assert _commit_exists(store) is False
        _assert_deletion_matches(store, "new-refresh")


def test_secret_save_failure_is_pre_commit_old_authoritative() -> None:
    with TemporaryDirectory() as directory:
        tmp = Path(directory)
        secret = _MemorySecretStore()
        store = _make_store(tmp, secret)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))

        def failing_save(key: str, value: bytes) -> None:
            raise OSError("secret store unavailable")

        with patch.object(secret, "save", side_effect=failing_save):
            with pytest.raises(OSError, match="secret store unavailable"):
                session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "old-refresh"
        assert session.state.refresh_token == "old-refresh"
        assert _commit_exists(store) is False
        _assert_deletion_matches(store, "old-refresh")
        session.accept_tokens(_tokens("new"))
        assert _secret_refresh(secret) == "new-refresh"
        assert session.state.refresh_token == "new-refresh"
