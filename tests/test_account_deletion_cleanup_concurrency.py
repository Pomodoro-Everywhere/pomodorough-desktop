from __future__ import annotations

import json
import multiprocessing
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from pomodorough import network, network_account
from pomodorough.network import TokenStore
from pomodorough.network_account import (
    AccountLifecycle,
    _DeletionCleanup,
    _DeletionCredentialIdentity,
)
from pomodorough.network_session import ApiError, SessionState
from pomodorough.secure_store import SecureStoreError

API = "https://deletion-race.example.test"


class MarkerTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def account_deletion_cleanup_path(self) -> Path:
        return self.path


class _ConfirmedRearmRace:
    def __init__(
        self, lifecycle: AccountLifecycle, store: TokenStore, tamper: str
    ) -> None:
        self.lifecycle = lifecycle
        self.tamper = tamper
        self.confirmed = threading.Event()
        self.release = threading.Event()
        self.save_attempting = threading.Event()
        self.save_done = threading.Event()
        self.failures: list[BaseException] = []
        self.original_confirm = store.confirm_account_deletion_locked
        self.original_save_lock = network._account_deletion_cleanup_lock

    def request(self, *_args, **_kwargs) -> dict[str, object]:
        path = self.lifecycle._deletion_cleanup_path()
        if self.tamper == "unlink":
            path.unlink()
        else:
            path.write_text(
                json.dumps(network_account._CLEARED_DELETION_CLEANUP),
                encoding="utf-8",
            )
        return {}

    def paused_confirm(self, *args):
        result = self.original_confirm(*args)
        self.confirmed.set()
        assert self.release.wait(10)
        return result

    @contextmanager
    def observed_save_lock(self, path):
        self.save_attempting.set()
        with self.original_save_lock(path):
            yield

    def run(self, operation, done=None) -> None:
        try:
            operation()
        except BaseException as error:  # noqa: BLE001 - Thread reports all failures.
            self.failures.append(error)
        finally:
            if done is not None:
                done.set()


def _lifecycle(root_text: str) -> AccountLifecycle:
    root = Path(root_text)
    return AccountLifecycle(
        API,
        SessionState(),
        MarkerTokenStore(root / ".account-deletion"),
        lambda *_args, **_kwargs: {},
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        object(),
    )


def _cleanup(name: str, generation: int) -> _DeletionCleanup:
    identity = _DeletionCredentialIdentity.from_tokens(
        API, f"{name}-access", f"{name}-refresh"
    )
    return _DeletionCleanup(API, generation, identity)


def _tokens(name: str) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def _save_durable_credentials(root_text: str, name: str, results: Any) -> None:
    root = Path(root_text)
    store = TokenStore("durable-rotation", None, root / "session.json")
    store.bind_api(API)
    with patch("pomodorough.network.shutil.which", return_value=None):
        _reported(results, lambda: store.save(_tokens(name)))


def _durable_deletion_lifecycle(
    root: Path,
    *,
    seed_credentials: bool = True,
) -> tuple[AccountLifecycle, SessionState, TokenStore]:
    store = TokenStore("durable-rotation", None, root / "session.json")
    store.bind_api(API)
    if seed_credentials:
        with patch("pomodorough.network.shutil.which", return_value=None):
            store.save(_tokens("old"))
    state = SessionState(
        access_token="old-access",
        refresh_token="old-refresh",
        access_expires_at=datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC),
        authenticated=True,
    )
    lifecycle = AccountLifecycle(
        API, state, store, Mock(return_value={}), lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC), store.revocations,
    )
    return lifecycle, state, store


def _reported(results: Any, operation: Any) -> None:
    try:
        operation()
    except BaseException as error:  # noqa: BLE001 - Child must report every exit path.
        results.put((type(error).__name__, str(error)))
    else:
        results.put(("ok", ""))


def _paused_replace_writer(
    root_text: str,
    cleanup: _DeletionCleanup,
    replacing: Any,
    release: Any,
    results: Any,
) -> None:
    lifecycle = _lifecycle(root_text)
    original_replace = network_account._replace_file_for_durable_commit

    def paused_replace(
        source: Path, destination: Path, filesystem: Any = os
    ) -> None:
        replacing.set()
        if not release.wait(10):
            raise RuntimeError("replace release was not signaled")
        original_replace(source, destination, filesystem)

    with patch.object(
        network_account, "_replace_file_for_durable_commit", paused_replace
    ):
        _reported(results, lambda: lifecycle._write_deletion_cleanup(cleanup))


def _observed_writer(
    root_text: str,
    cleanup: _DeletionCleanup,
    attempting: Any,
    results: Any,
) -> None:
    lifecycle = _lifecycle(root_text)
    original_lock = network_account.token_store_lock

    @contextmanager
    def observed_lock(*args: Any, **kwargs: Any):
        attempting.set()
        with original_lock(*args, **kwargs):
            yield

    with patch.object(network_account, "token_store_lock", observed_lock):
        _reported(results, lambda: lifecycle._write_deletion_cleanup(cleanup))


def _paused_remover(
    root_text: str,
    cleanup: _DeletionCleanup,
    compared: Any,
    release: Any,
    results: Any,
) -> None:
    lifecycle = _lifecycle(root_text)
    original_read = lifecycle._read_deletion_cleanup_locked

    def paused_read(path: Path) -> _DeletionCleanup | None:
        current = original_read(path)
        compared.set()
        if not release.wait(10):
            raise RuntimeError("unlink release was not signaled")
        return current

    lifecycle._read_deletion_cleanup_locked = paused_read
    _reported(results, lambda: lifecycle._remove_deletion_cleanup(cleanup))


def _crash_after_durable_replace(
    root_text: str, cleanup: _DeletionCleanup
) -> None:
    lifecycle = _lifecycle(root_text)
    original_sync = lifecycle._sync_deletion_cleanup_directory

    def crash_after_sync(path: Path) -> None:
        original_sync(path)
        os._exit(23)

    lifecycle._sync_deletion_cleanup_directory = crash_after_sync
    lifecycle._write_deletion_cleanup(cleanup)


def _join(*processes: multiprocessing.Process) -> None:
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(3)
        assert not process.is_alive()


def test_process_compare_replace_has_one_durable_winner(tmp_path):
    context = multiprocessing.get_context("spawn")
    replacing, release, attempting = [context.Event() for _index in range(3)]
    winner_result, loser_result = context.Queue(), context.Queue()
    winner_cleanup, loser_cleanup = _cleanup("winner", 7), _cleanup("loser", 8)
    winner = context.Process(
        target=_paused_replace_writer,
        args=(str(tmp_path), winner_cleanup, replacing, release, winner_result),
    )
    loser = context.Process(
        target=_observed_writer,
        args=(str(tmp_path), loser_cleanup, attempting, loser_result),
    )
    winner.start()
    try:
        assert replacing.wait(10)
        loser.start()
        assert attempting.wait(10)
    finally:
        release.set()
        _join(winner, loser)
    assert winner.exitcode == loser.exitcode == 0
    assert winner_result.get(timeout=3) == ("ok", "")
    assert loser_result.get(timeout=3) == (
        "SecureStoreError",
        "Another account deletion cleanup is pending.",
    )
    assert _lifecycle(str(tmp_path))._read_deletion_cleanup() == winner_cleanup


def test_process_compare_unlink_preserves_replacement(tmp_path):
    lifecycle = _lifecycle(str(tmp_path))
    removed_cleanup, replacement = _cleanup("removed", 9), _cleanup("new", 10)
    lifecycle._write_deletion_cleanup(removed_cleanup)
    context = multiprocessing.get_context("spawn")
    compared, release, attempting = [context.Event() for _index in range(3)]
    remove_result, write_result = context.Queue(), context.Queue()
    remover = context.Process(
        target=_paused_remover,
        args=(str(tmp_path), removed_cleanup, compared, release, remove_result),
    )
    writer = context.Process(
        target=_observed_writer,
        args=(str(tmp_path), replacement, attempting, write_result),
    )
    remover.start()
    try:
        assert compared.wait(10)
        writer.start()
        assert attempting.wait(10)
    finally:
        release.set()
        _join(remover, writer)
    assert remover.exitcode == writer.exitcode == 0
    assert remove_result.get(timeout=3) == ("ok", "")
    assert write_result.get(timeout=3) == ("ok", "")
    assert lifecycle._read_deletion_cleanup() == replacement


def test_crash_releases_lock_and_restart_preserves_generation(tmp_path):
    context = multiprocessing.get_context("spawn")
    crashed_cleanup = _cleanup("crashed", 11)
    process = context.Process(
        target=_crash_after_durable_replace,
        args=(str(tmp_path), crashed_cleanup),
    )
    process.start()
    _join(process)
    assert process.exitcode == 23
    restarted = _lifecycle(str(tmp_path))
    assert restarted._read_deletion_cleanup() == crashed_cleanup
    with pytest.raises(SecureStoreError, match="Another account deletion"):
        restarted._write_deletion_cleanup(_cleanup("replacement", 12))
    restarted._remove_deletion_cleanup(crashed_cleanup)
    restarted._write_deletion_cleanup(_cleanup("replacement", 12))
    assert restarted._read_deletion_cleanup().generation == 12


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError("lock rejected"), "lock is unavailable"),
        (ModuleNotFoundError("fcntl"), "locking is unsupported"),
    ],
)
def test_lock_acquisition_failure_never_mutates_marker(
    tmp_path, error, message
):
    lifecycle = _lifecycle(str(tmp_path))
    cleanup = _cleanup("blocked", 13)
    with (
        patch.object(network_account, "token_store_lock", side_effect=error),
        pytest.raises(SecureStoreError, match=message),
    ):
        lifecycle._write_deletion_cleanup(cleanup)
    assert lifecycle._read_deletion_cleanup() is None


def test_unlock_failure_is_reported_after_durable_replace(tmp_path):
    lifecycle = _lifecycle(str(tmp_path))
    cleanup = _cleanup("unlock", 14)

    @contextmanager
    def failing_unlock(*_args: Any, **_kwargs: Any):
        yield
        raise OSError("unlock rejected")

    with (
        patch.object(network_account, "token_store_lock", failing_unlock),
        pytest.raises(OSError, match="unlock rejected"),
    ):
        lifecycle._write_deletion_cleanup(cleanup)
    assert lifecycle._read_deletion_cleanup() == cleanup


def test_token_save_lock_failure_has_no_silent_success(tmp_path):
    store = TokenStore("race", None, tmp_path / "session.json")
    store.bind_api(API)
    old = {
        "refreshToken": "old-refresh",
        "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
    }
    replacement = old | {"refreshToken": "replacement-refresh"}
    with patch("pomodorough.network.shutil.which", return_value=None):
        store.save(old)
        with (
            patch(
                "pomodorough.network._account_deletion_cleanup_lock",
                side_effect=OSError("lock rejected"),
            ),
            pytest.raises(OSError, match="lock rejected"),
        ):
            store.save(replacement)
    assert store.load()["refreshToken"] == "old-refresh"


def test_process_rotation_before_marker_rejects_and_preserves_replacement(tmp_path):
    lifecycle, state, store = _durable_deletion_lifecycle(tmp_path)
    request = Mock(return_value={})
    lifecycle.request = request
    credentials = lifecycle.begin_deletion("DELETE")
    assert credentials is not None
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    rotation = context.Process(
        target=_save_durable_credentials,
        args=(str(tmp_path), "rotated", results),
    )
    rotation.start()
    _join(rotation)
    assert rotation.exitcode == 0 and results.get(timeout=3) == ("ok", "")
    assert state.refresh_token == "old-refresh"

    with pytest.raises(ApiError, match="sign_in_cancelled"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    request.assert_not_called()
    assert lifecycle._read_deletion_cleanup() is None
    assert store.load()["refreshToken"] == "rotated-refresh"

    restarted, _state, restarted_store = _durable_deletion_lifecycle(
        tmp_path,
        seed_credentials=False,
    )
    restarted.retry_sign_out_cleanup(0)
    assert restarted_store.load()["refreshToken"] == "rotated-refresh"
    assert restarted._read_deletion_cleanup() is None


@pytest.mark.parametrize("tamper", ["unlink", "cleared"])
def test_confirmed_rearm_serializes_concurrent_credential_save(tmp_path, tamper):
    lifecycle, _state, store = _durable_deletion_lifecycle(tmp_path)
    credentials = lifecycle.begin_deletion("DELETE")
    assert credentials is not None
    race = _ConfirmedRearmRace(lifecycle, store, tamper)
    lifecycle.request = race.request
    with (
        patch.object(store, "confirm_account_deletion_locked", race.paused_confirm),
        patch.object(network, "_account_deletion_cleanup_lock", race.observed_save_lock),
        patch("pomodorough.network.shutil.which", return_value=None),
    ):
        deletion = threading.Thread(
            target=race.run,
            args=(lambda: lifecycle.delete_account(credentials, Mock()),),
        )
        deletion.start()
        assert race.confirmed.wait(10)
        saving = threading.Thread(
            target=race.run,
            args=(lambda: store.save(_tokens("replacement")), race.save_done),
        )
        saving.start()
        assert race.save_attempting.wait(10)
        assert not race.save_done.wait(0.1)
        race.release.set()
        deletion.join(10)
        saving.join(10)
    assert not deletion.is_alive() and not saving.is_alive()
    assert race.failures == []
    assert store.load()["refreshToken"] == "replacement-refresh"
    assert lifecycle._read_deletion_cleanup() is None
