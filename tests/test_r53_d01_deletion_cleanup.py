"""Production deletion wiring with real SQLite, token storage, and Qt slots."""
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QInputDialog

from pomodorough.network_session import ApiError
from pomodorough.storage import Store
from test_r52_d01_reset_fence import (
    _blocked_workers, _quiet, _seed, _service, _window, _close,
    _deny, _dump, _process, _reopen, PRIVATE, OWNER,
)
from test_r52_d01_reset_fence import app as app  # pytest fixture
from test_signout_cleanup_recovery import FileVault, fixture_environment


@contextmanager
def workspace(application, root):
    with fixture_environment(root), _quiet(), _blocked_workers():
        vault = FileVault(root)
        store = Store(root / "state.sqlite3", iroh_secret_store=vault)
        _seed(store)
        tokens, cloud = _service(root, store, vault)
        window, notices, _ = _window(application, store, cloud)
        try:
            yield store, tokens, cloud, window, notices
        finally:
            store.connection.set_authorizer(None)
            _close(cloud, window, store)


def delete(cloud, window, request):
    cloud._accounts.request = request

    def run(work, success, failure):
        try:
            result = work()
        except Exception as error:
            failure(error)
        else:
            success(result)

    with patch.object(cloud, "_start", side_effect=run):
        with patch.object(QInputDialog, "getText", return_value=("DELETE", True)):
            window._delete_account_action()


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("fault", [None, "delete", "commit"])
def test_remote_completion_quarantines_and_retries(app, tmp_path, uncertain, fault):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        denials, observations = [], []

        def request(*args, **kwargs):
            observations.append((store.get_meta("accountDeletionCleanup"), tokens.load()))
            assert window._mutation_blocked()
            assert window.tasks == []
            if fault:
                store.connection.set_authorizer(_deny(fault, denials))
            if uncertain:
                raise ApiError("synthetic connection reset")
            return {}

        delete(cloud, window, request)
        assert observations[0][0] == {"version": 1}
        assert observations[0][1] is not None
        assert tokens.load() is None
        assert not cloud.authenticated
        if fault:
            assert denials and notices
            assert not store.connection.in_transaction
            assert store.get_meta("accountDeletionCleanup") is not None
            assert store.get_meta("snapshot")["user"] == OWNER
            before = _dump(store)
            window._delete_task(PRIVATE["id"])
            window._issue("start")
            assert _dump(store) == before
            assert window.tasks == []
            store.connection.set_authorizer(None)
            window.account_button.click()
        assert store.get_meta("accountDeletionCleanup") is None
        assert store.load()["snapshot"]["user"] is None
        assert window.tasks == []
        assert not window._mutation_blocked()
    assert _reopen(tmp_path)["tokens"] is None
    assert _reopen(tmp_path)["user"] is None


def test_definitive_http_denial_preserves_workspace(app, tmp_path):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        before = tokens.load()

        def request(*args, **kwargs):
            raise ApiError("synthetic denial", status=403)

        delete(cloud, window, request)
        assert cloud.authenticated
        assert tokens.load() == before
        assert store.load()["snapshot"]["user"] == OWNER
        assert any(task["id"] == PRIVATE["id"] for task in window.tasks)
        assert not window._mutation_blocked()
        assert store.get_meta("accountDeletionCleanup") is None


def test_reopen_without_credentials_recovers_pending_scrub(app, tmp_path):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        def request(*args, **kwargs):
            store.connection.set_authorizer(_deny("delete", []))
            return {}

        delete(cloud, window, request)
        assert tokens.load() is None
        assert store.get_meta("accountDeletionCleanup") is not None
    with fixture_environment(tmp_path), _quiet(), _blocked_workers():
        vault = FileVault(tmp_path)
        store = Store(tmp_path / "state.sqlite3", iroh_secret_store=vault)
        from pomodorough.network import CloudService, TokenStore
        tokens = TokenStore(store.device_id, vault, tmp_path / "session.json")
        cloud = CloudService(store.device_id, token_store=tokens)
        window, _, _ = _window(app, store, cloud)
        try:
            assert window._mutation_blocked()
            assert window.tasks == []
            window.account_resolution._retry_deletion_callback()
            _process(app)
            assert store.get_meta("accountDeletionCleanup") is None
            assert store.load()["snapshot"]["user"] is None
            assert not window._mutation_blocked()
        finally:
            _close(cloud, window, store)


def test_obligation_commit_denial_never_sends_delete(app, tmp_path):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        before = (_dump(store), tokens.load())
        requests, denials = [], []
        store.connection.set_authorizer(_deny("commit", denials))
        delete(cloud, window, lambda *a, **k: requests.append(a) or {})
        store.connection.set_authorizer(None)
        assert denials and notices
        assert not requests
        assert (_dump(store), tokens.load()) == before
        assert cloud.authenticated
        assert not window._mutation_blocked()


def test_pending_remote_request_cannot_be_scrubbed_or_unfenced(app, tmp_path):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        with patch.object(cloud, "_start", return_value=None):
            cloud.delete_account("DELETE")
        before = (_dump(store), tokens.load())
        window.account_button.click()
        window._session_expired()
        window._signed_in(OWNER)
        assert window._mutation_blocked()
        assert (_dump(store), tokens.load()) == before
        assert window.tasks == []


def test_reopen_prepared_but_unsent_preserves_account(app, tmp_path):
    with workspace(app, tmp_path) as (store, tokens, cloud, window, notices):
        assert window.account_resolution.prepare_deletion()
        before = tokens.load()
    with fixture_environment(tmp_path), _quiet(), _blocked_workers():
        vault = FileVault(tmp_path)
        store = Store(tmp_path / "state.sqlite3", iroh_secret_store=vault)
        tokens, cloud = _service(tmp_path, store, vault)
        window, _, _ = _window(app, store, cloud)
        try:
            window.account_resolution._retry_deletion_callback()
            assert store.get_meta("accountDeletionCleanup") is None
            assert store.get_meta("snapshot")["user"] == OWNER
            assert tokens.load() == before
            assert not window._mutation_blocked()
        finally:
            _close(cloud, window, store)
