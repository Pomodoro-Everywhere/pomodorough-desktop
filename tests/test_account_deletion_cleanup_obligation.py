from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough import network_account
from pomodorough.network import CloudService, TokenStore
from pomodorough.network_account import AccountLifecycle
from pomodorough.network_session import ApiError, SessionState
from pomodorough.secure_store import SecureStoreError, TokenCleanupPendingError

API = "https://deletion-cleanup.example.test"
REPLACEMENT_API = "https://replacement-account.example.test"


def tokens(name: str) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


class FailingDeleteVault:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.reject_delete = False

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        if self.reject_delete:
            raise OSError("credential deletion rejected")
        self.values.pop(key, None)


def account(tmp_path: Path, request=None, vault=None):
    request = request or Mock()
    secret_vault = vault or FailingDeleteVault()
    store = TokenStore("deletion-cleanup", secret_vault, tmp_path / "session.json")
    store.bind_api(API)
    store.save(tokens("old"))
    state = SessionState(
        access_token="old-access",
        refresh_token="old-refresh",
        access_expires_at=datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC),
        authenticated=True,
    )
    lifecycle = AccountLifecycle(
        API,
        state,
        store,
        request,
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        store.revocations,
    )
    return lifecycle, state, store, secret_vault


def begin(lifecycle: AccountLifecycle):
    credentials = lifecycle.begin_deletion("DELETE")
    assert credentials is not None
    return credentials


def run_immediately(function, on_result, on_error=None) -> None:
    try:
        on_result(function())
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)


def vault_refresh(store: TokenStore, vault: FailingDeleteVault) -> str:
    document = json.loads(vault.values[store.secret_key])
    return document["refreshToken"]


def assert_cleanup_resolved(lifecycle: AccountLifecycle) -> None:
    assert lifecycle._read_deletion_cleanup() is None


def tamper_deletion_marker(lifecycle: AccountLifecycle, tamper: str) -> None:
    path = lifecycle._deletion_cleanup_path()
    if tamper == "unlink":
        path.unlink()
        return
    if tamper == "changed":
        document = json.loads(path.read_text(encoding="utf-8"))
        document["generation"] += 1
        path.write_text(json.dumps(document), encoding="utf-8")
        return
    path.write_text(
        json.dumps(network_account._CLEARED_DELETION_CLEANUP), encoding="utf-8"
    )


def restarted_account(tmp_path: Path, vault: FailingDeleteVault) -> AccountLifecycle:
    store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    store.bind_api(API)
    return AccountLifecycle(
        API,
        SessionState(),
        store,
        Mock(),
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        store.revocations,
    )


def test_remote_delete_observes_durable_cleanup_obligation(tmp_path):
    observed = []
    lifecycle, state, store, vault = account(tmp_path)
    credentials = begin(lifecycle)

    def request(*_args, **_kwargs):
        path = lifecycle._deletion_cleanup_path()
        document = path.read_text(encoding="utf-8")
        observed.append(
            (path.exists(), vault_refresh(store, vault), "old-refresh" not in document)
        )
        return {}

    lifecycle.request = request
    assert lifecycle.delete_account(credentials, Mock()) == {}
    assert lifecycle.complete_deletion(credentials)
    assert observed == [(True, "old-refresh", True)]
    assert store.load() is None
    assert_cleanup_resolved(lifecycle)
    assert not state.authenticated


def test_remote_success_never_completes_when_secret_delete_fails(tmp_path):
    request = Mock(return_value={})
    lifecycle, state, store, vault = account(tmp_path, request)
    credentials = begin(lifecycle)
    vault.reject_delete = True

    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    assert request.call_count == 1
    assert not state.authenticated
    with pytest.raises(SecureStoreError, match="will be retried"):
        lifecycle.require_authentication_ready()
    assert vault_refresh(store, vault) == "old-refresh"
    assert store.secret_key in vault.values
    assert lifecycle._deletion_cleanup_path().exists()


@pytest.mark.parametrize("tamper", ["unlink", "cleared", "changed"])
def test_confirmed_delete_rearms_tampered_marker_and_cold_restart(
    tmp_path, tamper
):
    lifecycle, state, store, vault = account(tmp_path)

    def request(*_args, **_kwargs):
        tamper_deletion_marker(lifecycle, tamper)
        return {}

    lifecycle.request = request
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials) and not state.authenticated
    assert lifecycle._read_deletion_cleanup() is not None

    tamper_deletion_marker(lifecycle, tamper)
    with pytest.raises(SecureStoreError, match="must finish"):
        store.save(tokens("blocked"))
    restarted = restarted_account(tmp_path, vault)
    with pytest.raises(SecureStoreError, match="will be retried"):
        restarted.require_authentication_ready()
    cleanup = restarted._read_deletion_cleanup()
    assert cleanup is not None and cleanup.generation == credentials.generation
    vault.reject_delete = False
    restarted.require_authentication_ready()
    assert store.load() is None
    assert_cleanup_resolved(restarted)


@pytest.mark.parametrize("tamper", ["unlink", "cleared"])
def test_confirmed_delete_rearm_failure_stays_bound_to_credentials(tmp_path, tamper):
    lifecycle, state, store, vault = account(tmp_path)
    original_replace = lifecycle._replace_deletion_cleanup_locked
    replacements = 0

    def fail_rearm(path, document):
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("rearm rejected")
        original_replace(path, document)

    def request(*_args, **_kwargs):
        tamper_deletion_marker(lifecycle, tamper)
        return {}

    lifecycle.request = request
    credentials = begin(lifecycle)
    with (
        patch.object(lifecycle, "_replace_deletion_cleanup_locked", fail_rearm),
        pytest.raises(TokenCleanupPendingError, match="will be retried"),
    ):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials) and not state.authenticated
    assert lifecycle._read_deletion_cleanup() is None
    with pytest.raises(SecureStoreError, match="must finish"):
        store.save(tokens("blocked"))

    vault.reject_delete = True
    restarted = restarted_account(tmp_path, vault)
    with pytest.raises(SecureStoreError, match="will be retried"):
        restarted.require_authentication_ready()
    assert restarted._read_deletion_cleanup() is not None


def test_stored_refresh_token_is_covered_when_memory_token_is_missing(tmp_path):
    request = Mock(return_value={})
    lifecycle, state, store, vault = account(tmp_path, request)
    with state.lock:
        state.refresh_token = None
    credentials = begin(lifecycle)
    assert credentials.refresh_token is None
    vault.reject_delete = True

    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    assert request.call_count == 1
    assert not state.authenticated
    with pytest.raises(SecureStoreError, match="will be retried"):
        lifecycle.require_authentication_ready()
    assert vault_refresh(store, vault) == "old-refresh"
    assert store.secret_key in vault.values
    assert lifecycle._deletion_cleanup_path().exists()


def test_cloud_reports_cleanup_pending_instead_of_deletion_success(tmp_path):
    application = QApplication.instance() or QApplication([])
    vault = FailingDeleteVault()
    store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    cloud = CloudService(
        "deletion-cleanup", API, token_store=store, request=Mock(return_value={})
    )
    deleted, failures = [], []
    cloud.account_deleted.connect(lambda: deleted.append(True))
    cloud.account_deletion_failed.connect(failures.append)
    cloud._start = run_immediately
    cloud.start_revision_stream = Mock()
    try:
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud._accept_tokens(tokens("old"))
        cloud.authenticated = True
        vault.reject_delete = True
        cloud.delete_account("DELETE")
        assert deleted == []
        assert failures == [
            "Remote account deleted. Local credential cleanup will be retried."
        ]
        assert not cloud.authenticated
        assert cloud._accounts._deletion_cleanup_path().exists()
    finally:
        cloud.shutdown()
    assert application is not None


def test_cloud_rejects_success_after_unbound_durable_rotation(tmp_path):
    application = QApplication.instance() or QApplication([])
    vault = FailingDeleteVault()
    store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")

    def replace_durable_credentials(*_args, **_kwargs):
        replacement = tokens("replacement") | {"apiBase": API}
        vault.save(store.secret_key, json.dumps(replacement).encode("utf-8"))
        return {}

    cloud = CloudService(
        "deletion-cleanup", API, token_store=store, request=replace_durable_credentials
    )
    deleted, failures = [], []
    cloud.account_deleted.connect(lambda: deleted.append(True))
    cloud.account_deletion_failed.connect(failures.append)
    cloud._start = run_immediately
    cloud.start_revision_stream = Mock()
    try:
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud._accept_tokens(tokens("old"))
        cloud.authenticated = True
        cloud.delete_account("DELETE")
        assert deleted == []
        assert failures == [
            "Remote account deleted. Local credential cleanup will be retried."
        ]
        assert store.load()["refreshToken"] == "replacement-refresh"
        assert cloud._accounts._deletion_cleanup_path().exists()
    finally:
        cloud.shutdown()
    assert application is not None


def test_restart_retries_pending_remote_success_cleanup(tmp_path):
    lifecycle, _state, _store, vault = account(tmp_path, Mock(return_value={}))
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    vault.reject_delete = False

    restarted_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    restarted_store.bind_api(API)
    restarted = AccountLifecycle(
        API,
        SessionState(),
        restarted_store,
        Mock(),
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        restarted_store.revocations,
    )
    restarted.retry_sign_out_cleanup(0)
    assert restarted_store.secret_key not in vault.values
    assert restarted_store.load() is None
    assert_cleanup_resolved(restarted)


def test_restart_rearms_unreadable_obligation_before_cleanup(tmp_path):
    lifecycle, _state, _store, vault = account(tmp_path, Mock(return_value={}))
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    vault.reject_delete = False

    restarted_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    restarted_store.bind_api(API)
    restarted = AccountLifecycle(
        API,
        SessionState(),
        restarted_store,
        Mock(),
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        restarted_store.revocations,
    )
    cleanup_path = restarted._deletion_cleanup_path()
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == cleanup_path:
            raise PermissionError("temporarily unreadable")
        return original_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", read_text):
        restarted.retry_sign_out_cleanup(0)
    assert restarted_store.secret_key not in vault.values
    assert restarted_store.load() is None
    assert_cleanup_resolved(restarted)


def test_unreadable_obligation_never_clears_replacement_generation(tmp_path):
    lifecycle, state, store, vault = account(tmp_path, Mock(return_value={}))
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    vault.reject_delete = False
    lifecycle.retry_sign_out_cleanup(state.account_generation)
    store.save(tokens("replacement"))
    with state.lock:
        state.account_generation += 1
        state.access_token = "replacement-access"
        state.refresh_token = "replacement-refresh"
        state.authenticated = True

    cleanup_path = lifecycle._deletion_cleanup_path()
    cleanup_path.write_text("unreadable", encoding="utf-8")
    with pytest.raises(SecureStoreError, match="unreadable or malformed"):
        lifecycle.retry_sign_out_cleanup(state.account_generation)

    assert vault_refresh(store, vault) == "replacement-refresh"
    assert state.authenticated and state.refresh_token == "replacement-refresh"
    assert cleanup_path.read_text(encoding="utf-8") == "unreadable"


def test_structurally_corrupt_obligation_preserves_credentials_and_evidence(tmp_path):
    lifecycle, _state, store, vault = account(tmp_path)
    cleanup_path = lifecycle._deletion_cleanup_path()
    corrupt = {
        "version": 1,
        "apiBase": API,
        "generation": 1,
        "credentialState": "absent",
        "refreshTokenHash": "0" * 64,
    }
    encoded = json.dumps(corrupt)
    cleanup_path.write_text(encoded, encoding="utf-8")

    with pytest.raises(SecureStoreError, match="malformed"):
        lifecycle.require_authentication_ready()

    assert vault_refresh(store, vault) == "old-refresh"
    assert cleanup_path.read_text(encoding="utf-8") == encoded


def test_legacy_obligation_clears_only_after_credentials_are_absent(tmp_path):
    lifecycle, state, store, vault = account(tmp_path)
    cleanup_path = lifecycle._deletion_cleanup_path()
    legacy = {
        "version": 1,
        "cleanupState": "pending",
        "apiBase": API,
        "generation": 1,
        "credentialState": "refresh",
        "refreshTokenHash": AccountLifecycle._token_hash("old-refresh"),
    }
    cleanup_path.write_text(json.dumps(legacy), encoding="utf-8")
    vault.delete(store.secret_key)
    store._account_deletion_identity_path().unlink()

    lifecycle.require_authentication_ready()

    assert not cleanup_path.exists()
    assert state.authenticated and state.refresh_token == "old-refresh"


def test_legacy_obligation_never_clears_replacement_credentials(tmp_path):
    lifecycle, _state, store, vault = account(tmp_path)
    cleanup_path = lifecycle._deletion_cleanup_path()
    store.save(tokens("replacement"))
    legacy = {
        "version": 1,
        "cleanupState": "pending",
        "apiBase": API,
        "generation": 1,
        "credentialState": "refresh",
        "refreshTokenHash": AccountLifecycle._token_hash("old-refresh"),
    }
    cleanup_path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(SecureStoreError, match="will be retried"):
        lifecycle.require_authentication_ready()

    assert vault_refresh(store, vault) == "replacement-refresh"
    assert json.loads(cleanup_path.read_text(encoding="utf-8")) == legacy


def test_rotation_before_marker_rejects_and_restart_preserves_account(tmp_path):
    request = Mock(return_value={})
    lifecycle, state, _store, vault = account(tmp_path, request)
    credentials = begin(lifecycle)
    rotated = tokens("rotated")
    rotated_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    rotated_store.bind_api(API)
    rotated_store.save(rotated)
    assert state.refresh_token == "old-refresh"
    with pytest.raises(ApiError, match="sign_in_cancelled"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    request.assert_not_called()
    assert_cleanup_resolved(lifecycle)

    restarted_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    restarted_store.bind_api(API)
    restarted = AccountLifecycle(
        API,
        SessionState(),
        restarted_store,
        Mock(),
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        restarted_store.revocations,
    )
    restarted.retry_sign_out_cleanup(0)
    assert restarted_store.secret_key in vault.values
    assert restarted_store.load()["refreshToken"] == "rotated-refresh"
    assert_cleanup_resolved(restarted)


def test_cloud_startup_timer_retries_pending_deletion_cleanup(tmp_path):
    _application = QApplication.instance() or QApplication([])
    lifecycle, _state, _store, vault = account(tmp_path, Mock(return_value={}))
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    vault.reject_delete = False

    restarted_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    cloud = CloudService(
        "deletion-cleanup", API, token_store=restarted_store, request=Mock()
    )
    try:
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud._sign_out_cleanup_timer.timeout.emit()
        assert restarted_store.secret_key not in vault.values
        assert restarted_store.load() is None
        assert_cleanup_resolved(cloud._accounts)
    finally:
        cloud.shutdown()


def test_known_remote_failure_removes_obligation_and_preserves_retry(tmp_path):
    request = Mock(side_effect=ApiError("unavailable", 503))
    lifecycle, state, store, _vault = account(tmp_path, request)
    credentials = begin(lifecycle)

    with pytest.raises(ApiError, match="unavailable"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    assert state.authenticated and state.refresh_token == "old-refresh"
    assert store.load()["refreshToken"] == "old-refresh"
    assert_cleanup_resolved(lifecycle)


def test_uncertain_remote_failure_clears_credentials_fail_closed(tmp_path):
    lifecycle, state, store, vault = account(
        tmp_path,
        Mock(side_effect=ApiError("connection lost")),
    )
    credentials = begin(lifecycle)

    with pytest.raises(ApiError, match="connection lost"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    assert not state.authenticated and state.refresh_token is None
    assert store.load() is None
    assert store.secret_key not in vault.values


def test_uncertain_failure_clears_stored_only_refresh_fail_closed(tmp_path):
    lifecycle, state, store, vault = account(
        tmp_path,
        Mock(side_effect=ApiError("connection lost")),
    )
    with state.lock:
        state.refresh_token = None
    credentials = begin(lifecycle)

    with pytest.raises(ApiError, match="connection lost"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    assert not state.authenticated and state.refresh_token is None
    assert store.load() is None
    assert store.secret_key not in vault.values


@pytest.mark.parametrize("replacement_name", ["new", "old"])
def test_late_success_cannot_clear_replacement_generation(tmp_path, replacement_name):
    lifecycle, state, store, _vault = account(tmp_path)
    credentials = begin(lifecycle)
    replacement = tokens(replacement_name)

    def replace_account(*_args, **_kwargs):
        lifecycle.require_authentication_ready()
        with state.lock:
            store.save(replacement)
            state.access_token = replacement["accessToken"]
            state.refresh_token = replacement["refreshToken"]
            state.authenticated = True
        return {}

    lifecycle.request = replace_account
    with pytest.raises(ApiError, match="sign_in_cancelled"):
        lifecycle.delete_account(credentials, Mock())
    assert store.load()["refreshToken"] == replacement["refreshToken"]
    assert state.authenticated and state.refresh_token == replacement["refreshToken"]
    assert_cleanup_resolved(lifecycle)


def test_remote_success_preserves_cross_process_replacement(tmp_path):
    lifecycle, state, _store, vault = account(tmp_path)
    credentials = begin(lifecycle)
    replacement_store = TokenStore(
        "deletion-cleanup", vault, tmp_path / "session.json"
    )
    replacement_store.bind_api(API)

    def replace_from_new_session(*_args, **_kwargs):
        restarted = AccountLifecycle(
            API,
            SessionState(),
            replacement_store,
            Mock(),
            lambda key: key,
            lambda: datetime(2026, 9, 1, tzinfo=UTC),
            replacement_store.revocations,
        )
        restarted.require_authentication_ready()
        replacement_store.save(tokens("replacement"))
        return {}

    lifecycle.request = replace_from_new_session
    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    assert replacement_store.load()["refreshToken"] == "replacement-refresh"
    assert not state.authenticated
    assert_cleanup_resolved(lifecycle)


def test_remote_success_preserves_preexisting_replacement_origin(tmp_path):
    request = Mock(return_value={})
    lifecycle, state, _store, vault = account(tmp_path, request)
    replacement_store = TokenStore(
        "deletion-cleanup", vault, tmp_path / "session.json"
    )
    replacement_store.bind_api(REPLACEMENT_API)
    replacement_store.save(tokens("replacement"))

    with pytest.raises(ApiError, match="sign_in_cancelled"):
        lifecycle.begin_deletion("DELETE")
    request.assert_not_called()
    assert replacement_store.load()["refreshToken"] == "replacement-refresh"
    assert state.authenticated and state.refresh_token == "old-refresh"
    assert_cleanup_resolved(lifecycle)


def test_access_only_delete_rejects_unbound_same_origin_replacement(tmp_path):
    vault = FailingDeleteVault()
    store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    store.bind_api(API)
    state = SessionState(
        access_token="old-access",
        access_expires_at=datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC),
        authenticated=True,
    )
    lifecycle = AccountLifecycle(
        API,
        state,
        store,
        Mock(),
        lambda key: key,
        lambda: datetime(2026, 9, 1, tzinfo=UTC),
        store.revocations,
    )
    replacement = {
        "apiBase": API,
        "refreshToken": "replacement-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }
    vault.save(store.secret_key, json.dumps(replacement).encode("utf-8"))

    with pytest.raises(SecureStoreError, match="identity is unavailable"):
        lifecycle.begin_deletion("DELETE")
    lifecycle.request.assert_not_called()
    assert store.load()["refreshToken"] == "replacement-refresh"
    assert state.authenticated and state.access_token == "old-access"
    assert_cleanup_resolved(lifecycle)


def test_startup_restore_and_auth_wait_for_failed_cleanup(tmp_path):
    application = QApplication.instance() or QApplication([])
    lifecycle, _state, _store, vault = account(tmp_path, Mock(return_value={}))
    credentials = begin(lifecycle)
    vault.reject_delete = True
    with pytest.raises(TokenCleanupPendingError):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)

    restarted_store = TokenStore("deletion-cleanup", vault, tmp_path / "session.json")
    request = Mock()
    cloud = CloudService(
        "deletion-cleanup", API, token_store=restarted_store, request=request
    )
    failures = []
    cloud.failure.connect(failures.append)
    cloud._start = run_immediately
    try:
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        with patch.object(restarted_store, "load", wraps=restarted_store.load) as load:
            cloud.restore()
            load.assert_not_called()
        with pytest.raises(SecureStoreError, match="will be retried"):
            cloud._ensure_access()
        with pytest.raises(SecureStoreError, match="will be retried"):
            cloud._accept_login_tokens(tokens("replacement"))
        assert request.call_count == 0
        assert failures == [
            "Account deletion credential cleanup failed and will be retried."
        ]
        assert restarted_store.secret_key in vault.values
        assert cloud._accounts._deletion_cleanup_path().exists()

        vault.reject_delete = False
        cloud.restore()
        assert restarted_store.secret_key not in vault.values
        assert_cleanup_resolved(cloud._accounts)
    finally:
        cloud.shutdown()
    assert application is not None


def test_obligation_write_failure_prevents_remote_delete(tmp_path):
    request = Mock(return_value={})
    lifecycle, state, store, _vault = account(tmp_path, request)
    credentials = begin(lifecycle)
    with (
        patch(
            "pomodorough.network_account._replace_file_for_durable_commit",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    request.assert_not_called()
    assert state.authenticated
    assert store.load()["refreshToken"] == "old-refresh"
