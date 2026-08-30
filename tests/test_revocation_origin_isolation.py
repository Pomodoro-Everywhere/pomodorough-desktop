from __future__ import annotations

import json
import time
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication
from test_network_revocation import PlatformKeyring, token_response
from test_secure_store import linux_secret_store
from test_storage_revocation import credentials

from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.network_account import RevocationState
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError
from pomodorough.storage_revocation import PendingSessionRevocations

API = "https://origin.example.test"
OTHER_API = "https://replacement.example.test"


@pytest.fixture
def clouds(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    vault = PlatformKeyring()
    monkeypatch.setattr("pomodorough.secure_store.shutil.which", lambda _: "/bin/secret-tool")
    monkeypatch.setattr(PlatformSecretStore, "_run", staticmethod(vault.run))
    services = []

    def create(api=API, request=None, device="device"):
        tokens = TokenStore(device, PlatformSecretStore(tmp_path), tmp_path / f"{device}.json")
        service = CloudService(device, api, token_store=tokens, request=request or Mock(return_value={}))
        services.append(service)
        return service

    with linux_secret_store(tmp_path):
        yield create, app, vault
        for service in services:
            service.shutdown()
        assert QThreadPool.globalInstance().waitForDone(3000)


def wait_for_workers(app, service, request):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if request.called and not service._revocation_workers:
            return
        time.sleep(0.002)
    pytest.fail("Revocation worker did not finish")


@pytest.mark.parametrize("api", [API, OTHER_API], ids=["same-origin-control", "changed-origin"])
def test_startup_logout_uses_stored_origin_before_restore(clouds, api):
    create, app, _vault = clouds
    previous = create()
    previous._accept_login_tokens(token_response("origin"))
    previous.shutdown()
    request = Mock(side_effect=ApiError("offline", 503))
    restarted = create(api, request)
    restarted.logout()
    wait_for_workers(app, restarted, request)
    request.assert_called_once_with("POST", API + "/api/v1/auth/refresh", {"refreshToken": "origin-refresh"})
    assert restarted.token_store.load() is None
    pending = restarted.token_store.revocations.load(API)
    assert next(iter(pending.values()))["refreshToken"] == "origin-refresh"
    if api != API:
        assert restarted.token_store.revocations.load(api) == {}


@pytest.mark.parametrize("api", [API, OTHER_API], ids=["same-api-control", "changed-api"])
def test_real_qt_restart_retries_original_api_obligation(clouds, api):
    create, app, _vault = clouds
    previous = create()
    previous.token_store.revocations.save(API, "signed-out-session", credentials("old"))
    previous.shutdown()
    request = Mock(return_value={})
    restarted = create(api, request)
    wait_for_workers(app, restarted, request)
    request.assert_called_once_with("POST", API + "/api/v1/auth/logout", {}, access_token="old-access")
    assert restarted.token_store.revocations.load(API) == {}


def test_restore_never_refreshes_credentials_at_replacement_api(clouds):
    create, _app, _vault = clouds
    previous = create()
    previous._accept_login_tokens(token_response("origin"))
    previous.shutdown()
    request = Mock()
    restarted = create(OTHER_API, request)
    assert restarted.token_store.load() is None
    with pytest.raises(ApiError):
        restarted._ensure_access()
    with patch.object(restarted, "_start", side_effect=lambda function, *_args: function()):
        restarted.restore()
    request.assert_not_called()


def test_all_origins_with_same_identifier_resume_without_touching_current_session(clouds):
    create, app, _vault = clouds
    current_api = "https://current.example.test"
    request = Mock(return_value={})
    current = create(current_api, request)
    current._accept_login_tokens(token_response("current"))
    pending = current.token_store.revocations
    pending.save(API, "same-id", credentials("first"))
    pending.save(OTHER_API, "same-id", credentials("second"))
    wait_for_workers(app, current, request)
    assert {(call.args[1], call.kwargs["access_token"]) for call in request.call_args_list} == {
        (API + "/api/v1/auth/logout", "first-access"),
        (OTHER_API + "/api/v1/auth/logout", "second-access"),
    }
    assert request.call_count == 2
    assert pending.load(API) == pending.load(OTHER_API) == {}
    assert current.token_store.load()["refreshToken"] == "current-refresh"


def test_rotated_credentials_keep_captured_origin_through_retry_and_restart(clouds):
    create, _app, _vault = clouds
    previous = create()
    previous.shutdown()
    state = previous._accounts.revocation(None, "old-refresh", False)
    request = Mock(side_effect=[token_response("rotated"), ApiError("offline", 503)])
    replacement = create(OTHER_API, request)
    with pytest.raises(ApiError):
        replacement._accounts.revoke(state)
    assert {call.args[1] for call in request.call_args_list} == {
        API + "/api/v1/auth/refresh", API + "/api/v1/auth/logout",
    }
    replacement.shutdown()
    restarted = create(OTHER_API, Mock(return_value={}))
    restored = restarted._accounts.pending_revocations()
    assert len(restored) == 1
    assert restored[0].api_base == API
    assert restored[0].refresh_token == "rotated-refresh"
    restarted._accounts.revoke(restored[0])
    restarted._request.assert_called_once_with("POST", API + "/api/v1/auth/logout", {}, access_token="rotated-access")


@pytest.mark.parametrize("origin", [None, 12, "", "ftp://origin.test", "https://user:secret@origin.test", "https://origin.test/#fragment"])
def test_malformed_origin_fails_closed_without_clearing_credentials(clouds, origin):
    create, _app, _vault = clouds
    request = Mock()
    service = create(OTHER_API, request)
    stored = {"refreshToken": "unknown-refresh", "apiBase": origin}
    service.token_store.secret_store.save(service.token_store.secret_key, json.dumps(stored).encode())
    assert service.token_store.load() is None
    with pytest.raises(SecureStoreError):
        service.logout()
    request.assert_not_called()
    assert not service.token_store.fallback_path.exists()


def test_unbound_legacy_credentials_are_never_assigned_current_origin(clouds):
    create, _app, _vault = clouds
    request = Mock()
    service = create(OTHER_API, request)
    service.token_store.secret_store.save(service.token_store.secret_key, b'{"refreshToken":"legacy"}')
    assert service.token_store.load() is None
    service.logout()
    request.assert_not_called()
    assert service.token_store.revocations.load_all(OTHER_API) == {OTHER_API: {}}


def test_legacy_in_memory_revocation_binds_once_not_on_each_retry(clouds):
    create, _app, _vault = clouds
    old = create(API, Mock(side_effect=ApiError("offline", 503)))
    state = RevocationState("access", "refresh", True)
    with pytest.raises(ApiError):
        old._accounts.revoke(state)
    replacement = create(OTHER_API, Mock(return_value={}))
    replacement._accounts.revoke(state)
    replacement._request.assert_called_once_with("POST", API + "/api/v1/auth/logout", {}, access_token="access")


def test_reusing_bound_token_store_for_another_origin_is_rejected(clouds):
    create, _app, _vault = clouds
    service = create()
    with pytest.raises(SecureStoreError, match="already bound"):
        service.token_store.bind_api(OTHER_API)


def test_observed_legacy_queue_becomes_discoverable_from_new_origin(clouds):
    create, _app, _vault = clouds
    service = create()
    pending = service.token_store.revocations
    pending._secrets.save(pending._key(API), json.dumps({"version": 1, "pending": {"old": credentials("old")}}).encode())
    assert pending.load_all(API)[API] == {"old": credentials("old")}
    restarted = PendingSessionRevocations(pending._secrets, "device")
    assert restarted.load_all(OTHER_API)[API] == {"old": credentials("old")}
