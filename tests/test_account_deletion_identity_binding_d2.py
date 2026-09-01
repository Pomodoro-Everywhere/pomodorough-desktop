from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from pomodorough import network_account
from pomodorough.network import TokenStore
from pomodorough.network_account import AccountLifecycle, _DeletionCredentialIdentity
from pomodorough.network_session import ApiError, SessionState
from pomodorough.secure_store import SecureStoreError, TokenCleanupPendingError

API = "https://deletion-identity.example.test"
OTHER_API = "https://replacement-identity.example.test"


class MemoryVault:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def tokens(name: str, *, refresh_token: str | None = None) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": refresh_token or f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def account(
    tmp_path: Path,
    request=None,
    vault: MemoryVault | None = None,
    *,
    seed_credentials: bool = True,
) -> tuple[
    AccountLifecycle, SessionState, TokenStore, MemoryVault
]:
    request = request or Mock()
    vault = vault or MemoryVault()
    store = TokenStore("d2-identity", vault, tmp_path / "session.json")
    store.bind_api(API)
    if seed_credentials:
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
    return lifecycle, state, store, vault


def replacement_cases() -> list[object]:
    return [
        pytest.param(API, "replacement-access", "replacement-refresh", id="same-origin"),
        pytest.param(OTHER_API, "replacement-access", "replacement-refresh", id="cross-origin"),
        pytest.param(API, "replacement-access", "old-refresh", id="same-refresh"),
    ]


def replace_credentials(
    store: TokenStore,
    vault: MemoryVault,
    api_base: str,
    access_token: str,
    refresh_token: str,
) -> dict[str, str]:
    document = {
        "apiBase": api_base,
        "refreshToken": refresh_token,
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }
    vault.save(store.secret_key, json.dumps(document).encode())
    identity = _DeletionCredentialIdentity.from_tokens(
        api_base, access_token, refresh_token
    )
    identity_document = {
        "version": 1,
        "apiBase": identity.api_base,
        "accessTokenHash": identity.access_token_hash,
        "refreshTokenHash": identity.refresh_token_hash,
    }
    store._write_private_file(
        store._account_deletion_identity_path(),
        json.dumps(identity_document, separators=(",", ":"), sort_keys=True),
    )
    return document


def durable_credentials(store: TokenStore, vault: MemoryVault) -> dict[str, str]:
    return json.loads(vault.values[store.secret_key])


def begin(lifecycle: AccountLifecycle):
    credentials = lifecycle.begin_deletion("DELETE")
    assert credentials is not None
    return credentials


def tamper_marker(lifecycle: AccountLifecycle, tamper: str) -> None:
    path = lifecycle._deletion_cleanup_path()
    if tamper == "unlink":
        path.unlink()
        return
    path.write_text(
        json.dumps(network_account._CLEARED_DELETION_CLEANUP), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("replacement_api", "replacement_access", "refresh_token"),
    replacement_cases(),
)
def test_replacement_before_marker_rejects_without_network(
    tmp_path, replacement_api, replacement_access, refresh_token
):
    request = Mock(return_value={})
    lifecycle, state, store, vault = account(tmp_path, request)
    credentials = begin(lifecycle)
    replacement = replace_credentials(
        store, vault, replacement_api, replacement_access, refresh_token
    )

    with pytest.raises(ApiError, match="sign_in_cancelled"):
        lifecycle.delete_account(credentials, Mock())

    assert lifecycle.fail_deletion(credentials)
    request.assert_not_called()
    assert durable_credentials(store, vault) == replacement
    assert lifecycle._read_deletion_cleanup() is None
    assert state.authenticated and state.access_token == "old-access"


@pytest.mark.parametrize(
    ("replacement_api", "replacement_access", "refresh_token"),
    replacement_cases(),
)
def test_replacement_after_marker_blocks_success_and_restart_cleanup(
    tmp_path, replacement_api, replacement_access, refresh_token
):
    lifecycle, state, store, vault = account(tmp_path)
    replacement = {}

    def request(*_args, **_kwargs):
        replacement.update(
            replace_credentials(
                store, vault, replacement_api, replacement_access, refresh_token
            )
        )
        return {}

    lifecycle.request = request
    credentials = begin(lifecycle)
    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials)
    assert not state.authenticated
    assert durable_credentials(store, vault) == replacement
    assert lifecycle._read_deletion_cleanup() is not None

    restarted, _state, _store, _vault = account(
        tmp_path, vault=vault, seed_credentials=False
    )
    with pytest.raises(SecureStoreError, match="will be retried"):
        restarted.require_authentication_ready()
    assert durable_credentials(store, vault) == replacement


@pytest.mark.parametrize("tamper", ["unlink", "cleared"])
def test_tampered_marker_preserves_same_origin_replacement(tmp_path, tamper):
    lifecycle, state, store, vault = account(tmp_path)
    replacement = {}

    def request(*_args, **_kwargs):
        tamper_marker(lifecycle, tamper)
        replacement.update(
            replace_credentials(
                store, vault, API, "replacement-access", "replacement-refresh"
            )
        )
        return {}

    lifecycle.request = request
    credentials = begin(lifecycle)
    with pytest.raises(TokenCleanupPendingError, match="will be retried"):
        lifecycle.delete_account(credentials, Mock())
    assert lifecycle.fail_deletion(credentials) and not state.authenticated
    assert durable_credentials(store, vault) == replacement

    restarted, _state, restarted_store, _vault = account(
        tmp_path, vault=vault, seed_credentials=False
    )
    restarted.require_authentication_ready()
    restarted_store.save(tokens("next"))
    assert restarted_store.load()["refreshToken"] == "next-refresh"


@pytest.mark.parametrize(
    ("replacement_api", "replacement_access", "refresh_token"),
    replacement_cases(),
)
def test_known_failure_preserves_replacement_and_original_error(
    tmp_path, replacement_api, replacement_access, refresh_token
):
    lifecycle, state, store, vault = account(tmp_path)
    replacement = {}

    def request(*_args, **_kwargs):
        replacement.update(
            replace_credentials(
                store, vault, replacement_api, replacement_access, refresh_token
            )
        )
        raise ApiError("server unavailable", 503)

    lifecycle.request = request
    credentials = begin(lifecycle)
    with pytest.raises(ApiError, match="server unavailable") as raised:
        lifecycle.delete_account(credentials, Mock())
    assert raised.value.status == 503
    assert lifecycle.fail_deletion(credentials)
    assert state.authenticated and state.access_token == "old-access"
    assert durable_credentials(store, vault) == replacement
    assert lifecycle._read_deletion_cleanup() is None


def test_401_rotation_rebinds_marker_and_cleans_rotated_credentials(tmp_path):
    lifecycle, state, store, vault = account(tmp_path)
    calls = []

    def request(method, _url, _payload, access_token=None):
        calls.append((method, access_token))
        if method == "POST":
            return tokens("rotated")
        if access_token == "old-access":
            raise ApiError("expired", 401)
        return {}

    def accept(response):
        store.save(response)
        state.access_token = response["accessToken"]
        state.refresh_token = response["refreshToken"]
        state.access_expires_at = datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC)

    lifecycle.request = request
    credentials = begin(lifecycle)
    assert lifecycle.delete_account(credentials, accept) == {}
    assert lifecycle.complete_deletion(credentials)
    assert calls == [
        ("DELETE", "old-access"),
        ("POST", None),
        ("DELETE", "rotated-access"),
    ]
    assert store.secret_key not in vault.values
    assert lifecycle._read_deletion_cleanup() is None
    assert not state.authenticated


@pytest.mark.parametrize(
    ("replacement_api", "replacement_access", "refresh_token"),
    replacement_cases(),
)
def test_401_refresh_rejects_replacement_before_persisting_rotation(
    tmp_path, replacement_api, replacement_access, refresh_token
):
    lifecycle, state, store, vault = account(tmp_path)
    replacement = {}

    def request(method, _url, _payload, access_token=None):
        if method == "DELETE":
            raise ApiError("expired", 401)
        replacement.update(
            replace_credentials(
                store,
                vault,
                replacement_api,
                replacement_access,
                refresh_token,
            )
        )
        return tokens("rotated")

    def accept(response):
        store.save(response)
        state.access_token = response["accessToken"]
        state.refresh_token = response["refreshToken"]

    lifecycle.request = request
    credentials = begin(lifecycle)
    with pytest.raises(SecureStoreError, match="Authenticated account changed"):
        lifecycle.delete_account(credentials, accept)
    assert lifecycle.fail_deletion(credentials)
    assert state.authenticated and state.access_token == "old-access"
    assert durable_credentials(store, vault) == replacement
    assert lifecycle._read_deletion_cleanup() is None


@pytest.mark.parametrize("callback", ["complete", "fail"])
def test_stale_callback_cannot_publish_replacement_deletion(tmp_path, callback):
    lifecycle, state, store, vault = account(tmp_path)
    credentials = begin(lifecycle)
    replacement = replace_credentials(
        store, vault, API, "replacement-access", "replacement-refresh"
    )
    with state.lock:
        state.account_generation += 1
        state.access_token = "replacement-access"
        state.refresh_token = "replacement-refresh"
        state.authenticated = True
        state.deleting_account = False

    assert not getattr(lifecycle, f"{callback}_deletion")(credentials)
    assert state.authenticated and state.access_token == "replacement-access"
    assert durable_credentials(store, vault) == replacement


def test_identity_sidecar_preserves_primary_credential_contract(tmp_path):
    _lifecycle, _state, store, vault = account(tmp_path)

    assert durable_credentials(store, vault) == {
        "apiBase": API,
        "refreshToken": "old-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }
    with store.account_deletion_credential_identity() as identity:
        assert identity == _DeletionCredentialIdentity.from_tokens(
            API, "old-access", "old-refresh"
        )


@pytest.mark.parametrize("condition", ["missing", "corrupt", "mismatch"])
def test_untrusted_identity_sidecar_blocks_deletion_before_network(
    tmp_path, condition
):
    request = Mock(return_value={})
    lifecycle, state, store, vault = account(tmp_path, request)
    identity_path = store._account_deletion_identity_path()
    if condition == "missing":
        identity_path.unlink()
    elif condition == "corrupt":
        identity_path.write_text("not-json", encoding="utf-8")
    else:
        replacement = _DeletionCredentialIdentity.from_tokens(
            API, "replacement-access", "old-refresh"
        )
        document = {
            "version": 1,
            "apiBase": replacement.api_base,
            "accessTokenHash": replacement.access_token_hash,
            "refreshTokenHash": replacement.refresh_token_hash,
        }
        identity_path.write_text(json.dumps(document), encoding="utf-8")

    expected_error = ApiError if condition == "mismatch" else SecureStoreError
    with pytest.raises(expected_error):
        lifecycle.begin_deletion("DELETE")

    request.assert_not_called()
    assert durable_credentials(store, vault)["refreshToken"] == "old-refresh"
    assert state.authenticated and state.access_token == "old-access"
