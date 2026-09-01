from __future__ import annotations

import ctypes
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import Mock, patch

import pytest

from pomodorough import network_account
from pomodorough.network import TokenStore
from pomodorough.network_account import (
    AccountLifecycle,
    _DeletionCleanup,
    _DeletionCredentialIdentity,
)
from pomodorough.network_session import SessionState
from pomodorough.secure_store import SecureStoreError

API = "https://windows-durability.example.test"


class MarkerTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def account_deletion_cleanup_path(self) -> Path:
        return self.path


class CleanupTokenStore(MarkerTokenStore):
    def __init__(self, path: Path, events: list[str]) -> None:
        super().__init__(path)
        self.events = events

    @contextmanager
    def account_deletion_credentials_locked(self) -> Iterator[None]:
        yield

    def account_deletion_credential_identity_locked(
        self,
    ) -> _DeletionCredentialIdentity:
        return cleanup().identity

    def confirm_account_deletion_locked(
        self,
        _api_base: str,
        _generation: int,
        _identity: _DeletionCredentialIdentity,
    ) -> bool:
        return True

    def clear_account_deletion_credentials_locked(
        self,
        _api_base: str,
        _identity: _DeletionCredentialIdentity,
    ) -> bool:
        self.events.append("credentials")
        return True


class MoveFileEx:
    def __init__(self, result: int = 1, events: list[str] | None = None) -> None:
        self.result = result
        self.events = events
        self.calls: list[tuple[str, str, int]] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, source: str, destination: str, flags: int) -> int:
        self.calls.append((source, destination, flags))
        if self.events is not None:
            self.events.append("durable-marker")
        if self.result:
            os.replace(source, destination)
        return self.result


class FaultyCleanupFile:
    def __init__(self, cleanup_file: object, failure: str) -> None:
        self.cleanup_file = cleanup_file
        self.failure = failure

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup_file.close()
        if self.failure == "close":
            raise OSError("close failed")

    def write(self, value: str) -> int:
        if self.failure == "write":
            raise OSError("write failed")
        return self.cleanup_file.write(value)

    def flush(self) -> None:
        if self.failure == "flush":
            raise OSError("flush failed")
        self.cleanup_file.flush()

    def fileno(self) -> int:
        return self.cleanup_file.fileno()


def lifecycle(root: Path) -> AccountLifecycle:
    return lifecycle_with_store(MarkerTokenStore(root / ".account-deletion"))


def lifecycle_with_store(store: MarkerTokenStore) -> AccountLifecycle:
    return AccountLifecycle(
        API,
        SessionState(),
        store,
        lambda *_args, **_kwargs: {},
        lambda key: key,
        lambda: None,
        object(),
    )


def cleanup(generation: int = 7) -> _DeletionCleanup:
    identity = _DeletionCredentialIdentity.from_tokens(
        API, "access-token", "refresh-token"
    )
    return _DeletionCleanup(API, generation, identity)


def windows_kernel(move_file: MoveFileEx) -> SimpleNamespace:
    return SimpleNamespace(MoveFileExW=move_file)


@contextmanager
def windows_adapter(move_file: MoveFileEx) -> Iterator[None]:
    with (
        patch.object(network_account, "_platform_name", return_value="nt"),
        patch.object(
            network_account,
            "_load_windows_kernel32",
            return_value=windows_kernel(move_file),
        ),
    ):
        yield


def test_windows_loader_requests_last_error_tracking() -> None:
    kernel = object()
    with patch.object(ctypes, "WinDLL", create=True, return_value=kernel) as load:
        assert network_account._load_windows_kernel32() is kernel
    load.assert_called_once_with("kernel32", use_last_error=True)


def test_windows_replace_uses_atomic_write_through_flags(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    move_file = MoveFileEx()
    with patch.object(
        network_account, "_load_windows_kernel32", return_value=windows_kernel(move_file)
    ):
        network_account._replace_windows_write_through(source, destination)
    assert destination.read_text(encoding="utf-8") == "new"
    assert move_file.calls == [
        (
            str(source.absolute()),
            str(destination.absolute()),
            network_account._MOVEFILE_REPLACE_EXISTING
            | network_account._MOVEFILE_WRITE_THROUGH,
        )
    ]
    assert move_file.argtypes == [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    assert move_file.restype is ctypes.c_int


def test_windows_replace_failure_propagates_without_mutation(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    move_file = MoveFileEx(result=0)
    with (
        patch.object(
            network_account,
            "_load_windows_kernel32",
            return_value=windows_kernel(move_file),
        ),
        patch.object(network_account, "_last_windows_error", return_value=5),
        pytest.raises(OSError, match="write-through"),
    ):
        network_account._replace_windows_write_through(source, destination)
    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "old"


def test_windows_cleanup_uses_durable_cleared_marker(tmp_path: Path) -> None:
    account = lifecycle(tmp_path)
    obligation = cleanup()
    move_file = MoveFileEx()
    with windows_adapter(move_file):
        account._write_deletion_cleanup(obligation)
        account._remove_deletion_cleanup(obligation)
        assert account._read_deletion_cleanup() is None
    marker = account._deletion_cleanup_path()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "version": 1,
        "cleanupState": "cleared",
    }
    assert len(move_file.calls) == 2


def test_windows_clear_failure_preserves_pending_generation(tmp_path: Path) -> None:
    account = lifecycle(tmp_path)
    obligation = cleanup(8)
    account._write_deletion_cleanup(obligation)
    with (
        patch.object(network_account, "_platform_name", return_value="nt"),
        patch.object(
            network_account,
            "_replace_windows_write_through",
            side_effect=OSError("write-through failed"),
        ),
        pytest.raises(OSError, match="write-through failed"),
    ):
        account._remove_deletion_cleanup(obligation)
    assert account._read_deletion_cleanup() == obligation


def test_windows_resolution_orders_credentials_before_durable_clear(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    store = CleanupTokenStore(tmp_path / ".account-deletion", events)
    account = lifecycle_with_store(store)
    obligation = cleanup(9)
    account._write_deletion_cleanup(obligation)
    move_file = MoveFileEx(events=events)
    with windows_adapter(move_file):
        assert account._resolve_deletion_cleanup_locked(obligation)
    assert events == ["credentials", "durable-marker"]
    assert account._read_deletion_cleanup() is None


def test_exact_cleared_marker_allows_sign_in_but_variant_blocks(tmp_path: Path) -> None:
    store = TokenStore("windows-marker", None, tmp_path / "session.json")
    marker = store.account_deletion_cleanup_path()
    marker.write_text(
        json.dumps(network_account._CLEARED_DELETION_CLEANUP), encoding="utf-8"
    )
    response = {
        "refreshToken": "accepted",
        "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
    }
    with patch("pomodorough.network.shutil.which", return_value=None):
        store.save(response)
    marker.write_text(
        json.dumps(network_account._CLEARED_DELETION_CLEANUP | {"generation": 9}),
        encoding="utf-8",
    )
    with pytest.raises(SecureStoreError, match="must finish"):
        store.save(response | {"refreshToken": "rejected"})
    assert store.load()["refreshToken"] == "accepted"


def test_windows_fallback_replace_failure_has_no_silent_success(tmp_path: Path) -> None:
    store = TokenStore("windows-fallback", None, tmp_path / "session.json")
    store.fallback_path.write_text("old", encoding="utf-8")
    move_file = MoveFileEx(result=0)
    with (
        patch.object(network_account, "_platform_name", return_value="nt"),
        patch.object(
            network_account,
            "_load_windows_kernel32",
            return_value=windows_kernel(move_file),
        ),
        patch.object(network_account, "_last_windows_error", return_value=5),
        pytest.raises(OSError, match="write-through"),
    ):
        store._write_fallback("new")
    assert store.fallback_path.read_text(encoding="utf-8") == "old"


def test_windows_crash_after_replace_restarts_with_pending_marker(
    tmp_path: Path,
) -> None:
    account = lifecycle(tmp_path)
    obligation = cleanup(10)
    move_file = MoveFileEx()
    with (
        windows_adapter(move_file),
        patch.object(
            account,
            "_sync_deletion_cleanup_directory",
            side_effect=RuntimeError("simulated crash"),
        ),
        pytest.raises(RuntimeError, match="simulated crash"),
    ):
        account._write_deletion_cleanup(obligation)
    restarted = lifecycle(tmp_path)
    with windows_adapter(move_file):
        assert restarted._read_deletion_cleanup() == obligation
        restarted._remove_deletion_cleanup(obligation)
        assert restarted._read_deletion_cleanup() is None


def test_windows_stale_generation_cannot_clear_replacement(tmp_path: Path) -> None:
    account = lifecycle(tmp_path)
    replacement = cleanup(12)
    move_file = MoveFileEx()
    with windows_adapter(move_file):
        account._write_deletion_cleanup(replacement)
        account._remove_deletion_cleanup(cleanup(11))
        assert account._read_deletion_cleanup() == replacement
    assert len(move_file.calls) == 1


def test_windows_corrupt_marker_blocks_authentication_without_mutation(
    tmp_path: Path,
) -> None:
    store = TokenStore("windows-corrupt", None, tmp_path / "session.json")
    response = {
        "refreshToken": "accepted",
        "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
    }
    with patch("pomodorough.network.shutil.which", return_value=None):
        store.save(response)
    marker = store.account_deletion_cleanup_path()
    marker.write_text("{corrupt", encoding="utf-8")
    move_file = MoveFileEx()
    with (
        windows_adapter(move_file),
        pytest.raises(SecureStoreError, match="unreadable or malformed"),
    ):
        store.save(response | {"refreshToken": "replacement"})
    assert marker.read_text(encoding="utf-8") == "{corrupt"
    assert store.load()["refreshToken"] == "accepted"
    assert move_file.calls == []


def test_windows_lock_failure_never_mutates_marker(tmp_path: Path) -> None:
    account = lifecycle(tmp_path)
    move_file = MoveFileEx()
    with (
        windows_adapter(move_file),
        patch.object(
            network_account,
            "token_store_lock",
            side_effect=OSError("lock rejected"),
        ),
        pytest.raises(SecureStoreError, match="lock is unavailable"),
    ):
        account._write_deletion_cleanup(cleanup(13))
    assert not account._deletion_cleanup_path().exists()
    assert move_file.calls == []


@pytest.mark.parametrize("failure", ["write", "flush", "close"])
def test_windows_marker_stream_failure_has_no_silent_success(
    tmp_path: Path, failure: str
) -> None:
    account = lifecycle(tmp_path)
    move_file = MoveFileEx()
    real_fdopen = os.fdopen

    def faulty_fdopen(*args: object, **kwargs: object) -> FaultyCleanupFile:
        return FaultyCleanupFile(real_fdopen(*args, **kwargs), failure)

    with (
        windows_adapter(move_file),
        patch.object(network_account.os, "fdopen", faulty_fdopen),
        pytest.raises(OSError, match=failure),
    ):
        account._write_deletion_cleanup(cleanup(14))
    assert not account._deletion_cleanup_path().exists()
    assert move_file.calls == []


def test_windows_marker_fsync_failure_has_no_silent_success(tmp_path: Path) -> None:
    account = lifecycle(tmp_path)
    move_file = MoveFileEx()
    with (
        windows_adapter(move_file),
        patch.object(network_account.os, "fsync", side_effect=OSError("fsync failed")),
        pytest.raises(OSError, match="fsync failed"),
    ):
        account._write_deletion_cleanup(cleanup(15))
    assert not account._deletion_cleanup_path().exists()
    assert move_file.calls == []


def test_unknown_platform_fails_closed_before_replace(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    filesystem = SimpleNamespace(name="unsupported", replace=Mock())
    with pytest.raises(SecureStoreError, match="unsupported"):
        network_account._replace_file_for_durable_commit(
            source, destination, filesystem
        )
    filesystem.replace.assert_not_called()
    assert destination.read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(os.name != "nt", reason="Real Win32 contract requires Windows")
def test_real_windows_write_through_replace(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_text("new", encoding="utf-8")
    with source.open("rb") as source_file:
        os.fsync(source_file.fileno())
    destination.write_text("old", encoding="utf-8")
    network_account._replace_windows_write_through(source, destination)
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "new"
