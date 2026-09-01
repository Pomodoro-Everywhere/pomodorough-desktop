from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, call, patch, sentinel

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.network import ApiError, CloudService, TokenStore
from test_secure_store import MemorySecretStore


def _tokens(name: str, *, fresh: bool = True) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z" if fresh else "2000-01-01T00:00:00Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def _run_immediately(function, on_result, on_error=None) -> None:
    try:
        response = function()
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)
    else:
        on_result(response)


@pytest.fixture
def cloud_factory(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("pomodorough.network._oauth_secret_store", lambda: MemorySecretStore({}))
    monkeypatch.setattr("pomodorough.network.shutil.which", lambda _name: None)
    clouds = []

    def create():
        store = TokenStore("deletion-device", secret_store=None, fallback_path=tmp_path / "session.json")
        cloud = CloudService("deletion-device", "https://example.test", token_store=store, request=Mock())
        cloud._revocation_restore_timer.stop()
        monkeypatch.setattr(cloud, "start_revision_stream", Mock())
        monkeypatch.setattr(cloud, "stop_revision_stream", Mock())
        monkeypatch.setattr(cloud, "_start_revocation", Mock())
        monkeypatch.setattr(cloud, "_start", _run_immediately)
        clouds.append(cloud)
        return cloud

    yield create
    for cloud in clouds:
        cloud.shutdown()
    assert app is not None


def _install(cloud, name="original", *, fresh=False):
    cloud._accept_tokens(_tokens(name, fresh=fresh))
    cloud.authenticated = True


def _change_account(cloud, change):
    if change == "shutdown":
        cloud.shutdown()
        return
    cloud.logout()
    if change in ("switch", "new_deletion"):
        _install(cloud, "replacement", fresh=True)
        if change == "new_deletion":
            assert cloud._begin_account_deletion("DELETE") is not None


def _session_snapshot(cloud):
    return (
        cloud.access_token, cloud.refresh_token, cloud.authenticated,
        cloud.deleting_account, cloud._account_generation, cloud.token_store.load(),
    )


@pytest.mark.parametrize("initially_fresh", [False, True])
@pytest.mark.parametrize("retry_needs_refresh", [False, True])
def test_rotation_is_durable_before_delete_and_survives_503_retry(
    cloud_factory, initially_fresh, retry_needs_refresh,
):
    cloud = cloud_factory()
    _install(cloud, fresh=initially_fresh)
    failures, deleted = [], []
    cloud.account_deletion_failed.connect(failures.append)
    cloud.account_deleted.connect(lambda: deleted.append(True))

    def request(method, _url, payload, **kwargs):
        if method == "POST":
            assert payload == {"refreshToken": "original-refresh"}
            return _tokens("rotated")
        if kwargs["access_token"] == "original-access":
            raise ApiError("expired", 401)
        assert kwargs["access_token"] == "rotated-access"
        assert cloud.token_store.load()["refreshToken"] == "rotated-refresh"
        assert cloud.refresh_token == "rotated-refresh"
        raise ApiError("unavailable", 503)

    cloud._request.side_effect = request
    cloud.delete_account("DELETE")
    assert failures == ["unavailable"]
    assert cloud.authenticated and not cloud.deleting_account
    assert cloud.access_token == "rotated-access"
    assert cloud.token_store.load()["refreshToken"] == "rotated-refresh"
    cloud.start_revision_stream.assert_called_once_with()
    cloud._request.reset_mock(side_effect=True)
    cloud._request.side_effect = [_tokens("retry"), {}] if retry_needs_refresh else [{}]
    if retry_needs_refresh:
        cloud.access_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    cloud.delete_account("DELETE")
    expected = []
    if retry_needs_refresh:
        expected.append(call("POST", "https://example.test/api/v1/auth/refresh", {"refreshToken": "rotated-refresh"}))
    expected.append(call("DELETE", "https://example.test/api/v1/account", {"confirmation": "DELETE"},
                         access_token="retry-access" if retry_needs_refresh else "rotated-access"))
    assert cloud._request.call_args_list == expected
    assert deleted == [True]
    assert cloud.token_store.load() is None
    assert not cloud.authenticated


def test_restart_restores_rotated_refresh_after_delete_503(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    cloud._request.side_effect = [_tokens("rotated"), ApiError("unavailable", 503)]
    cloud.delete_account("DELETE")
    cloud.shutdown()

    restarted = cloud_factory()
    assert restarted.token_store is not cloud.token_store
    assert restarted.refresh_token is None
    restarted._request.side_effect = [_tokens("restored"), {"user": {"id": "original"}}, {}]
    restarted.restore()
    assert restarted.authenticated
    assert restarted._request.call_args_list[0] == call(
        "POST", "https://example.test/api/v1/auth/refresh", {"refreshToken": "rotated-refresh"},
    )
    restarted.delete_account("DELETE")
    assert restarted._request.call_args_list[-1].kwargs == {"access_token": "restored-access"}
    assert restarted.token_store.load() is None


@pytest.mark.parametrize("boundary", ["write", "replace", "sync"])
def test_rotation_persistence_failure_prevents_delete_and_memory_acceptance(cloud_factory, boundary):
    cloud = cloud_factory()
    _install(cloud)
    before = cloud.token_store.fallback_path.read_bytes()
    failures = []
    cloud.account_deletion_failed.connect(failures.append)
    cloud._request.return_value = _tokens("rotated")
    target = {
        "write": "tempfile.mkstemp",
        "replace": "_replace_file_for_durable_commit",
        "sync": "os.fsync",
    }[boundary]
    with patch(f"pomodorough.network.{target}", side_effect=OSError("storage unavailable")):
        cloud.delete_account("DELETE")
    assert failures == ["storage unavailable"]
    assert cloud._request.call_count == 1
    assert cloud._request.call_args.args[0] == "POST"
    assert cloud.access_token == "original-access"
    assert cloud.refresh_token == "original-refresh"
    assert cloud.authenticated and not cloud.deleting_account
    assert cloud.token_store.fallback_path.read_bytes() == before


def test_post_replace_failure_stops_delete_but_restart_keeps_rotation(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    cloud._request.return_value = _tokens("rotated")
    failures = []
    cloud.account_deletion_failed.connect(failures.append)
    file_sync = Mock(wraps=os.fsync)
    directory_syncs = 0

    def sync(descriptor):
        nonlocal directory_syncs
        if descriptor is sentinel.directory_descriptor:
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("directory sync failed")
            return None
        return file_sync(descriptor)

    directory_open = Mock(return_value=sentinel.directory_descriptor)
    directory_close = Mock()
    filesystem = SimpleNamespace(
        **{
            **vars(os),
            "name": "posix",
            "O_DIRECTORY": getattr(os, "O_DIRECTORY", 1 << 30),
            "open": directory_open,
            "close": directory_close,
            "fsync": sync,
        }
    )
    with patch("pomodorough.network.os", filesystem):
        cloud.delete_account("DELETE")
    assert file_sync.call_count == 2
    assert directory_open.call_count == 2
    directory_open.assert_called_with(cloud.token_store.fallback_path.parent,
                                      os.O_RDONLY | filesystem.O_DIRECTORY)
    assert directory_close.call_count == 2
    directory_close.assert_called_with(sentinel.directory_descriptor)
    assert failures == ["directory sync failed"]
    assert cloud._request.call_count == 1
    assert cloud.refresh_token == "original-refresh"
    restarted = cloud_factory()
    assert restarted.token_store.load()["refreshToken"] == "rotated-refresh"
    restarted._request.side_effect = [_tokens("restored"), {"user": {"id": "original"}}]
    restarted.restore()
    assert restarted.authenticated and restarted.refresh_token == "restored-refresh"


def test_rotation_without_directory_sync_persists_before_delete_and_restart(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)

    def request(method, *_args, **_kwargs):
        if method == "POST":
            return _tokens("rotated")
        assert cloud.token_store.load()["refreshToken"] == "rotated-refresh"
        raise ApiError("unavailable", 503)

    cloud._request.side_effect = request
    failures = []
    cloud.account_deletion_failed.connect(failures.append)
    filesystem = SimpleNamespace(**{name: value for name, value in vars(os).items() if name != "O_DIRECTORY"})
    filesystem.fsync = Mock(wraps=os.fsync)
    filesystem.open = Mock(side_effect=AssertionError("Directory descriptors are unavailable"))
    with patch("pomodorough.network.os", filesystem):
        cloud.delete_account("DELETE")
    assert filesystem.fsync.call_count == 2
    filesystem.open.assert_not_called()
    assert failures == ["unavailable"]
    assert cloud._request.call_args_list == [
        call("POST", "https://example.test/api/v1/auth/refresh", {"refreshToken": "original-refresh"}),
        call("DELETE", "https://example.test/api/v1/account", {"confirmation": "DELETE"},
             access_token="rotated-access"),
    ]
    assert cloud.authenticated and not cloud.deleting_account
    assert cloud.refresh_token == "rotated-refresh"
    restarted = cloud_factory()
    assert restarted.token_store.load()["refreshToken"] == "rotated-refresh"
    restarted._request.side_effect = [_tokens("restored"), {"user": {"id": "original"}}]
    restarted.restore()
    assert restarted._request.call_args_list[0] == call(
        "POST", "https://example.test/api/v1/auth/refresh", {"refreshToken": "rotated-refresh"},
    )
    assert restarted.authenticated and restarted.refresh_token == "restored-refresh"


@pytest.mark.parametrize("field", list(_tokens("rotated")))
@pytest.mark.parametrize("invalid", [None, "", "   ", 7])
def test_incomplete_or_invalid_pair_never_reaches_delete(cloud_factory, field, invalid):
    cloud = cloud_factory()
    _install(cloud)
    before = cloud.token_store.fallback_path.read_bytes()
    response = _tokens("rotated")
    if invalid is None:
        response.pop(field)
    else:
        response[field] = invalid
    cloud._request.return_value = response
    failures = []
    cloud.account_deletion_failed.connect(failures.append)
    cloud.delete_account("DELETE")
    assert len(failures) == 1
    assert cloud._request.call_count == 1
    assert cloud.refresh_token == "original-refresh"
    assert cloud.token_store.fallback_path.read_bytes() == before
    assert cloud.authenticated and not cloud.deleting_account


@pytest.mark.parametrize("field", ["accessTokenExpiresAt", "refreshTokenExpiresAt"])
@pytest.mark.parametrize("invalid", ["not-a-date", "2099-01-01T00:00:00"])
def test_invalid_expiry_never_reaches_delete(cloud_factory, field, invalid):
    cloud = cloud_factory()
    _install(cloud)
    cloud._request.return_value = {**_tokens("rotated"), field: invalid}
    cloud.delete_account("DELETE")
    assert cloud._request.call_count == 1
    assert cloud.refresh_token == "original-refresh"
    assert cloud.token_store.load()["refreshToken"] == "original-refresh"


@pytest.mark.parametrize("change", ["logout", "switch", "shutdown", "new_deletion"])
@pytest.mark.parametrize("refresh_fails", [False, True])
def test_account_change_during_refresh_fences_acceptance_and_error_callbacks(
    cloud_factory, change, refresh_fails,
):
    cloud = cloud_factory()
    _install(cloud)
    failures, deleted, after_change = [], [], []
    cloud.account_deletion_failed.connect(failures.append)
    cloud.account_deleted.connect(lambda: deleted.append(True))

    def refresh(*_args, **_kwargs):
        _change_account(cloud, change)
        after_change.append(_session_snapshot(cloud))
        if refresh_fails:
            raise ApiError("old refresh rejected", 401)
        return _tokens("stale")

    cloud._request.side_effect = refresh
    cloud.delete_account("DELETE")
    assert cloud._request.call_count == 1
    assert _session_snapshot(cloud) == after_change[0]
    assert not failures and not deleted
    cloud.start_revision_stream.assert_not_called()


@pytest.mark.parametrize("change", ["logout", "switch", "shutdown", "new_deletion"])
def test_late_delete_401_never_refreshes_after_account_change(cloud_factory, change):
    cloud = cloud_factory()
    _install(cloud, fresh=True)
    after_change, failures = [], []
    cloud.account_deletion_failed.connect(failures.append)

    def rejected_delete(*_args, **_kwargs):
        _change_account(cloud, change)
        after_change.append(_session_snapshot(cloud))
        raise ApiError("expired", 401)

    cloud._request.side_effect = rejected_delete
    cloud.delete_account("DELETE")
    assert cloud._request.call_count == 1
    assert cloud._request.call_args.args[0] == "DELETE"
    assert _session_snapshot(cloud) == after_change[0]
    assert not failures
    cloud.start_revision_stream.assert_not_called()


@pytest.mark.parametrize("change", ["logout", "switch", "shutdown", "new_deletion"])
@pytest.mark.parametrize("success", [False, True])
def test_late_completion_cannot_clear_replacement_or_finish_new_deletion(cloud_factory, change, success):
    cloud = cloud_factory()
    _install(cloud)
    cloud._start = Mock()
    cloud.delete_account("DELETE")
    function, on_result, on_error = cloud._start.call_args.args
    cloud._request.side_effect = [_tokens("rotated"), {}]
    response = function()
    _change_account(cloud, change)
    before = _session_snapshot(cloud)
    deleted, failed, statuses = [], [], []
    cloud.account_deleted.connect(lambda: deleted.append(True))
    cloud.account_deletion_failed.connect(failed.append)
    cloud.status_changed.connect(statuses.append)
    if success:
        on_result(response)
    else:
        on_error(ApiError("late failure", 503))
    assert _session_snapshot(cloud) == before
    assert not deleted and not failed and not statuses
    cloud.start_revision_stream.assert_not_called()


def test_account_change_before_worker_start_keeps_original_deletion_generation(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    cloud._start = Mock()
    cloud.delete_account("DELETE")
    function, on_result, on_error = cloud._start.call_args.args
    _change_account(cloud, "switch")
    before = _session_snapshot(cloud)
    with pytest.raises(ApiError, match="cancelled") as cancelled:
        function()
    on_error(cancelled.value)
    on_result({})
    cloud._request.assert_not_called()
    assert _session_snapshot(cloud) == before


def test_same_generation_refresh_replacement_cannot_be_overwritten(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)

    def refresh(*_args, **_kwargs):
        cloud._accept_tokens(_tokens("newer"))
        return _tokens("stale")

    cloud._request.side_effect = refresh
    cloud.delete_account("DELETE")
    assert cloud._request.call_count == 1
    assert cloud.refresh_token == "newer-refresh"
    assert json.loads(cloud.token_store.fallback_path.read_text())["refreshToken"] == "newer-refresh"


def test_replaced_refresh_is_rejected_before_captured_refresh_request(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    credentials = cloud._begin_account_deletion("DELETE")
    cloud._accept_tokens(_tokens("newer"))
    before = _session_snapshot(cloud)
    with pytest.raises(ApiError, match="cancelled"):
        cloud._refresh_deletion_access(credentials)
    cloud._request.assert_not_called()
    assert _session_snapshot(cloud) == before


def test_deletion_credentials_repr_hides_tokens(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    credentials = cloud._begin_account_deletion("DELETE")
    assert credentials.generation == cloud._account_generation
    assert "original-access" not in repr(credentials)
    assert "original-refresh" not in repr(credentials)


@pytest.mark.parametrize("change", ["logout", "switch", "shutdown", "new_deletion"])
def test_refresh_worker_cannot_accept_after_concurrent_account_change(cloud_factory, change):
    cloud = cloud_factory()
    _install(cloud)
    credentials = cloud._begin_account_deletion("DELETE")
    refreshing, respond = Event(), Event()

    def refresh(*_args, **_kwargs):
        refreshing.set()
        assert respond.wait(5)
        return _tokens("stale")

    cloud._request.side_effect = refresh
    with ThreadPoolExecutor(max_workers=1) as workers:
        deletion = workers.submit(cloud._delete_captured_account, credentials)
        try:
            assert refreshing.wait(5)
            _change_account(cloud, change)
            before = _session_snapshot(cloud)
        finally:
            respond.set()
        with pytest.raises(ApiError, match="cancelled"):
            deletion.result(timeout=5)
    assert cloud._request.call_count == 1
    assert _session_snapshot(cloud) == before


def test_rotation_save_holds_generation_lock_until_memory_acceptance(cloud_factory):
    cloud = cloud_factory()
    _install(cloud)
    credentials = cloud._begin_account_deletion("DELETE")
    saving, release, switching, switched = Event(), Event(), Event(), Event()
    cloud._request.side_effect = [_tokens("rotated"), {}]
    original_save = cloud.token_store.save

    def save(response):
        if response["refreshToken"] == "rotated-refresh":
            assert cloud._state.lock.locked()
            saving.set()
            assert release.wait(5)
        original_save(response)

    def switch():
        switching.set()
        cloud._accounts.sign_out()
        cloud._session.accept_login_tokens(_tokens("replacement"))
        cloud.authenticated = True
        switched.set()

    with patch.object(cloud.token_store, "save", side_effect=save), ThreadPoolExecutor(max_workers=2) as workers:
        deletion = workers.submit(cloud._delete_captured_account, credentials)
        try:
            assert saving.wait(5)
            replacement = workers.submit(switch)
            assert switching.wait(5)
            assert not switched.wait(0.05)
        finally:
            release.set()
        replacement.result(timeout=5)
        try:
            deletion.result(timeout=5)
        except ApiError as error:
            assert "cancelled" in str(error)
    cloud._account_deleted({}, credentials)
    assert cloud.authenticated
    assert cloud.access_token == "replacement-access"
    assert cloud.refresh_token == "replacement-refresh"
    assert cloud.token_store.load()["refreshToken"] == "replacement-refresh"
