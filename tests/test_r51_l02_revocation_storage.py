"""R51-L02: revocation queue storage failures report bounded diagnostics.

Startup restoration and replay use workers whose error callbacks only
schedule retries. Storage infrastructure failures now capture one
payload-free diagnostic per fixed operation/cause key while the durable
job, retry delays, and active session stay intact. Expected offline,
auth, cancellation, malformed, and stale outcomes stay quiet.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from pomodorough import sentry_monitoring as monitoring
from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.network_account import RevocationState
from pomodorough.secure_store import SecureStoreError

CANARY_TOKEN = "canary-refresh-TOKEN-abc123"
CANARY_PATH = "/tmp/canary-secret-path/session.json"
DEVICE = "device-r51l02"
API = "https://example.test"
FROZEN_NOW = 100.0
ORIGINS_PREFIX = "oauth-revocation-origins-v1:"
QUEUE_PREFIX = "oauth-revocations-v1:"


class _FailingSecrets:
    """In-memory secret store with prefix-scoped infrastructure faults."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_load_prefixes: set[str] = set()
        self.fail_save_prefixes: set[str] = set()
        self.fail_save_when_acknowledged = False
        self.malformed_queues = False

    def load(self, key: str) -> bytes | None:
        if self.malformed_queues and key.startswith(QUEUE_PREFIX):
            raise SecureStoreError("Stored pending revocations are malformed.")
        if key.startswith(tuple(self.fail_load_prefixes)):
            raise _infra()
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if self.fail_save_when_acknowledged and b'"pending":{}' in value:
            raise _infra()
        if key.startswith(tuple(self.fail_save_prefixes)):
            raise _infra()
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _infra() -> SecureStoreError:
    error = SecureStoreError(f"vault read failed {CANARY_TOKEN} at {CANARY_PATH}")
    error.__cause__ = OSError("keychain down")
    return error


def _seed(service: CloudService) -> RevocationState:
    return service._accounts.revocation("live-access", "live-refresh", True)


def _pending(service: CloudService) -> dict:
    return service.token_store.revocations.load(service.api_base)


def _capture_patch(calls: list[BaseException]) -> object:
    return patch(
        "pomodorough.sentry_monitoring.capture_exception", side_effect=calls.append
    )


def _frozen() -> object:
    return patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def setup(tmp_path: Path) -> Iterator[tuple[CloudService, _FailingSecrets]]:
    secrets = _FailingSecrets()
    store = TokenStore(DEVICE, secret_store=secrets, fallback_path=tmp_path / "s.json")
    store.bind_api(API)
    request = Mock(return_value={})
    service = CloudService(DEVICE, API, token_store=store, request=request)
    service._revocation_restore_timer.stop()
    service._sign_out_cleanup_timer.stop()
    service._state.authenticated = True
    try:
        yield service, secrets
    finally:
        service.shutdown()


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    monitoring.reset_revocation_storage_budget()
    monitoring.reset_capture_fallback_count()
    try:
        yield
    finally:
        monitoring.reset_revocation_storage_budget()
        monitoring.reset_capture_fallback_count()


def test_restore_origins_read_reports_once_and_queue_survives(setup) -> None:
    service, secrets = setup
    state = _seed(service)
    secrets.fail_load_prefixes = {ORIGINS_PREFIX}
    calls: list[BaseException] = []
    with _capture_patch(calls), _frozen():
        for _ in range(3):
            with pytest.raises(SecureStoreError):
                service._accounts.pending_revocations()
    assert len(calls) == 1
    assert str(calls[0]) == "revocation restore failed: queue-read"
    secrets.fail_load_prefixes = set()
    assert state.identifier in _pending(service)


def test_restore_queue_read_retries_with_backoff_then_drains(app, setup) -> None:
    service, secrets = setup
    _seed(service)
    secrets.fail_load_prefixes = {QUEUE_PREFIX}
    calls: list[BaseException] = []
    pool = QThreadPool.globalInstance()
    with (
        patch.object(pool, "start", side_effect=lambda worker: worker.run()),
        _capture_patch(calls),
        _frozen(),
    ):
        for _ in range(3):
            service._revocation_restore_timer.timeout.emit()
        assert service._revocation_restore_timer.interval() == 4000
        assert len(calls) == 1
        secrets.fail_load_prefixes = set()
        service._revocation_restore_timer.timeout.emit()
    assert _pending(service) == {}


def test_replay_queue_read_reports_and_job_retained(setup) -> None:
    service, secrets = setup
    state = _seed(service)
    secrets.fail_load_prefixes = {QUEUE_PREFIX}
    calls: list[BaseException] = []
    with _capture_patch(calls), _frozen():
        for _ in range(3):
            with pytest.raises(SecureStoreError):
                service._accounts.revoke(state)
    assert len(calls) == 1
    assert str(calls[0]) == "revocation replay failed: queue-read"
    secrets.fail_load_prefixes = set()
    assert state.identifier in _pending(service)


def test_replay_queue_update_reports_and_job_unacknowledged(setup) -> None:
    service, secrets = setup
    state = _seed(service)
    secrets.fail_save_prefixes = {QUEUE_PREFIX}
    calls: list[BaseException] = []
    with _capture_patch(calls), _frozen():
        with pytest.raises(SecureStoreError):
            service._accounts.revoke(state)
    assert len(calls) == 1
    assert str(calls[0]) == "revocation replay failed: queue-update"
    assert _pending(service)[state.identifier]["acknowledged"] is False
    secrets.fail_save_prefixes = set()


def test_replay_acknowledge_reports_then_drains_without_relogout(setup) -> None:
    service, secrets = setup
    state = _seed(service)
    request = service._accounts.request
    assert isinstance(request, Mock)
    secrets.fail_save_when_acknowledged = True
    calls: list[BaseException] = []
    with _capture_patch(calls), _frozen():
        with pytest.raises(SecureStoreError):
            service._accounts.revoke(state)
    assert len(calls) == 1
    assert str(calls[0]) == "revocation replay failed: acknowledge"
    assert _pending(service)[state.identifier]["acknowledged"] is True
    secrets.fail_save_when_acknowledged = False
    seen = request.call_count
    service._accounts.revoke(state)
    assert _pending(service) == {}
    assert request.call_count == seen


def test_storage_failure_leaves_active_session_untouched(setup) -> None:
    service, secrets = setup
    service.token_store.save(
        {
            "accessToken": "active-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "active-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
    )
    state = _seed(service)
    secrets.fail_load_prefixes = {QUEUE_PREFIX}
    calls: list[BaseException] = []
    with _capture_patch(calls), _frozen():
        with pytest.raises(SecureStoreError):
            service._accounts.revoke(state)
    assert len(calls) == 1
    secrets.fail_load_prefixes = set()
    assert service._state.authenticated is True
    assert service.token_store.load()["refreshToken"] == "active-refresh"


def test_offline_auth_cancel_malformed_stale_stay_quiet(setup) -> None:
    service, secrets = setup
    offline = _seed(service)
    no_refresh = service._accounts.revocation("stale-access", None, True)
    with patch("pomodorough.sentry_monitoring.capture_exception") as capture:
        with pytest.raises(ApiError):
            with patch.object(
                service._accounts,
                "_logout_request",
                side_effect=ApiError("Could not reach Pomodorough"),
            ):
                service._accounts.revoke(offline)
        with pytest.raises(ApiError):
            with patch.object(
                service._accounts,
                "_logout_request",
                side_effect=ApiError("expired", 401),
            ):
                service._accounts.revoke(no_refresh)
        with pytest.raises(ApiError):
            with patch.object(
                service._accounts,
                "_logout_request",
                side_effect=ApiError("sign in cancelled"),
            ):
                service._accounts.revoke(offline)
        secrets.malformed_queues = True
        try:
            with pytest.raises(SecureStoreError):
                service._accounts.pending_revocations()
        finally:
            secrets.malformed_queues = False
        capture.assert_not_called()
        service._accounts.revoke(offline)
        service._accounts.revoke(offline)
        service._accounts.revoke(no_refresh)
        capture.assert_not_called()
    assert _pending(service) == {}


def test_reporter_failure_counts_fallback_and_preserves_queue(setup) -> None:
    service, secrets = setup
    state = _seed(service)
    secrets.fail_load_prefixes = {QUEUE_PREFIX}
    before = monitoring.capture_fallback_count()
    with (
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=RuntimeError("sdk down"),
        ),
        _frozen(),
    ):
        for _ in range(3):
            with pytest.raises(SecureStoreError):
                service._accounts.revoke(state)
    assert monitoring.capture_fallback_count() == before + 1
    secrets.fail_load_prefixes = set()
    assert state.identifier in _pending(service)


def test_sdk_envelope_bounded_and_payload_free(setup) -> None:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    events: list[dict] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope) -> None:
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    service, secrets = setup
    state = service._accounts.revocation(CANARY_TOKEN, CANARY_TOKEN, True)
    secrets.fail_load_prefixes = {QUEUE_PREFIX}
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        transport=MemoryTransport,
        default_integrations=False,
        include_local_variables=False,
        send_client_reports=False,
        before_send=monitoring.scrub_sentry_event,
    )
    with patch.dict(os.environ, {"POMODOROUGH_SENTRY_ALLOW_IN_TESTS": "1"}), _frozen():
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            for _ in range(3):
                with pytest.raises(SecureStoreError):
                    service._accounts.revoke(state)
    client.close()
    secrets.fail_load_prefixes = set()
    assert len(events) == 1
    encoded = json.dumps(events[0])
    for secret in (CANARY_TOKEN, CANARY_PATH, DEVICE, API, state.identifier):
        assert secret not in encoded
    tags = events[0].get("tags") or {}
    assert tags.get("revocation_storage.operation") == "replay"
    assert tags.get("revocation_storage.cause") == "queue-read"
