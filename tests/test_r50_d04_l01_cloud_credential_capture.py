"""R50-D04/L01: sync/bootstrap hide credential infra failures.

Public login/sync/preview/resolve must classify unexpected storage-infra
failures with a fixed operation+cause budget and payload-free capture,
while expected offline/cancel/401/409/stale/malformed stay quiet.
Uses the real QThreadPool Worker path for the operation matrix.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.network import (
    ApiError,
    CloudService,
    TokenStore,
    _cloud_credential_cause,
)
from pomodorough.secure_store import SecureStoreError, TokenCommitPendingError
from pomodorough.sentry_monitoring import (
    capture_cloud_credential,
    reset_cloud_credential_budget,
)

CANARY_TOKEN = "canary-refresh-TOKEN-abc123"
CANARY_PATH = "/tmp/canary-secret-path/session.json"
API = "https://example.test"


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _valid_tokens() -> dict[str, str]:
    base = datetime(2099, 2, 3, 4, 5, 6, tzinfo=UTC)
    return {
        "accessToken": "new-access",
        "accessTokenExpiresAt": base.isoformat().replace("+00:00", "Z"),
        "refreshToken": "new-refresh",
        "refreshTokenExpiresAt": base.isoformat().replace("+00:00", "Z"),
    }


def _run_immediately(function, on_result, on_error=None) -> None:
    try:
        on_result(function())
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def cloud(tmp_path: Path) -> Iterator[CloudService]:
    service = CloudService(
        "device-1",
        API,
        token_store=TokenStore(
            "device-1",
            secret_store=_MemorySecretStore(),
            fallback_path=tmp_path / "session.json",
        ),
    )
    service._state.authenticated = True
    service._state.access_token = "old-access"
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    try:
        yield service
    finally:
        service.shutdown()


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    reset_cloud_credential_budget()
    try:
        yield
    finally:
        reset_cloud_credential_budget()


def _wired(service: CloudService) -> tuple[list, list, list]:
    statuses: list[str] = []
    failures: list[str] = []
    expired: list[bool] = []
    service.status_changed.connect(statuses.append)
    service.failure.connect(failures.append)
    service.session_expired.connect(lambda: expired.append(True))
    return statuses, failures, expired


def _pump_until(app: QApplication, done: list, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not done and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def _fake_request(expected_refresh: str, seen: list) -> object:
    def _request(method: str, url: str, payload=None, **kwargs):
        seen.append(url)
        if url.endswith("/auth/refresh"):
            assert payload == {"refreshToken": expected_refresh}
            return dict(_valid_tokens())
        if url.endswith("/api/v1/sync"):
            return {"ok": True}
        if url.endswith("/api/v1/bootstrap"):
            return {"mode": "none"}
        if url.endswith("/api/v1/bootstrap/resolve"):
            return {"ok": True}
        if url.endswith("/api/v1/me"):
            return {"user": {"id": "user-1"}}
        raise AssertionError(f"unexpected network reach: {url}")

    return _request


@pytest.mark.parametrize("operation", ["sync", "preview_bootstrap", "resolve_bootstrap"])
def test_real_worker_load_failure_reports_once_payload_free(app, cloud, operation) -> None:
    _, failures, _ = _wired(cloud)
    denied = PermissionError(13, f"denied {CANARY_TOKEN} at {CANARY_PATH}")
    seen: list[str] = []
    calls: list[BaseException] = []
    with (
        patch.object(cloud.token_store, "load", side_effect=denied),
        patch.object(cloud, "_request", side_effect=_fake_request("x", seen)),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda error: calls.append(error),
        ),
    ):
        if operation == "sync":
            cloud.sync({"ops": []})
        elif operation == "resolve_bootstrap":
            cloud.resolve_bootstrap({"choice": "keep"})
        else:
            cloud.preview_bootstrap()
        _pump_until(app, failures)
    assert len(failures) == 1 and str(denied) in failures[0]
    assert seen == []
    assert len(calls) == 1
    captured = calls[0]
    assert isinstance(captured, RuntimeError)
    assert str(captured) == f"cloud credential {operation} failed: filesystem"
    assert CANARY_TOKEN not in str(captured) and CANARY_PATH not in str(captured)
    assert not cloud._state.busy


def test_real_worker_login_infra_reports_once(app, cloud) -> None:
    _, failures, _ = _wired(cloud)
    cloud._state.authenticated = False
    cloud._state.access_token = None
    denied = PermissionError(13, f"denied {CANARY_TOKEN} at {CANARY_PATH}")
    calls: list[BaseException] = []
    with (
        patch.object(cloud, "_authorize_google", side_effect=denied),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda error: calls.append(error),
        ),
    ):
        cloud.login()
        _pump_until(app, failures)
    assert failures == [str(denied)]
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential login failed: filesystem"
    assert CANARY_TOKEN not in str(calls[0])


def test_refresh_save_failure_reports_secure_store_infra(cloud) -> None:
    _, failures, _ = _wired(cloud)
    seen: list[str] = []
    infra = SecureStoreError(f"vault read {CANARY_TOKEN}")
    infra.__cause__ = OSError(f"keychain down {CANARY_PATH}")
    stored = {"refreshToken": "stored", "apiBase": cloud.token_store.api_base}
    with (
        patch.object(cloud.token_store, "load", return_value=stored),
        patch.object(
            cloud._session, "request", side_effect=_fake_request("stored", seen)
        ),
        patch.object(cloud, "_request", side_effect=_fake_request("stored", seen)),
        patch.object(cloud.token_store, "save", side_effect=infra),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        cloud.sync({"ops": []})
    assert len(failures) == 1
    assert any(u.endswith("/auth/refresh") for u in seen)
    assert not any(u.endswith("/api/v1/sync") for u in seen)
    capture.assert_called_once()
    captured = capture.call_args.args[0]
    assert str(captured) == "cloud credential sync failed: secure-store-infra"


def test_bounded_budget_and_recovery(cloud) -> None:
    _, failures, _ = _wired(cloud)
    denied = PermissionError(13, "denied")
    with (
        patch.object(cloud.token_store, "load", side_effect=denied),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        cloud.sync({"ops": []})
        cloud._state.busy = False
        cloud._state.sync_queued = None
        cloud.sync({"ops": []})
        assert len(failures) == 2
        assert capture.call_count == 1
        cloud._state.authenticated = True
        from pomodorough.sentry_monitoring import note_cloud_credential_recovery

        note_cloud_credential_recovery()
        cloud._state.busy = False
        cloud._state.sync_queued = None
        cloud.sync({"ops": []})
        assert len(failures) == 3
        assert capture.call_count == 2
    assert capture.call_args.args[0] is not denied


def test_distinct_cause_starts_new_incident(cloud) -> None:
    _, failures, _ = _wired(cloud)
    denied = PermissionError(13, "denied")
    infra = SecureStoreError("vault down")
    infra.__cause__ = OSError("io down")
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        with patch.object(cloud.token_store, "load", side_effect=denied):
            cloud.sync({"ops": []})
        cloud._state.busy = False
        cloud._state.sync_queued = None
        with patch.object(cloud.token_store, "load", side_effect=infra):
            cloud.sync({"ops": []})
    assert len(failures) == 2
    assert capture.call_count == 2


def test_real_filesystem_directory_failure_reports_once(app, tmp_path) -> None:
    service = CloudService(
        "device-9",
        API,
        token_store=TokenStore(
            "device-9",
            secret_store=_MemorySecretStore(),
            fallback_path=tmp_path,
        ),
    )
    service._state.authenticated = True
    service._state.access_token = "old-access"
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    _, failures, _ = _wired(service)
    calls: list[BaseException] = []
    try:
        with patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda error: calls.append(error),
        ):
            service.sync({"ops": []})
            _pump_until(app, failures)
    finally:
        service.shutdown()
    assert len(failures) == 1
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential sync failed: filesystem"


def test_expected_and_malformed_stay_silent(cloud) -> None:
    _, failures, expired = _wired(cloud)
    cancel = ApiError(cloud.strings.text("network.error.sign_in_cancelled"))
    offline = ApiError("Could not reach Pomodorough")
    malformed = SecureStoreError("Stored OAuth credentials are malformed.")
    pending = TokenCommitPendingError("Secure credential commit is pending; retry.")
    cases = [
        offline,
        cancel,
        ApiError("expired", 401),
        ValueError("bad input"),
        KeyError("missing"),
        malformed,
        pending,
    ]
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        for error in cases:
            failures.clear()
            cloud._state.busy = False
            cloud._state.sync_queued = None
            cloud._state.authenticated = True
            with patch.object(cloud.token_store, "load", side_effect=error):
                cloud.sync({"ops": []})
            if isinstance(error, ApiError) and getattr(error, "status", None) == 401:
                assert expired == [True]
                assert failures == []
                expired.clear()
            else:
                assert failures == [str(error)]
        assert _cloud_credential_cause(pending) is None
        assert _cloud_credential_cause(malformed) is None
    capture.assert_not_called()


def test_409_and_401_stay_silent_for_bootstrap(cloud) -> None:
    _, failures, expired = _wired(cloud)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        with patch.object(
            cloud, "_authorized_request", side_effect=ApiError("conflict", 409)
        ):
            cloud.resolve_bootstrap({"choice": "keep"})
        assert failures == []
        cloud._state.busy = False
        with patch.object(
            cloud, "_authorized_request", side_effect=ApiError("expired", 401)
        ):
            cloud.preview_bootstrap()
        assert expired == [True]
    capture.assert_not_called()


def test_reporter_failure_stays_nonfatal(cloud) -> None:
    _, failures, _ = _wired(cloud)
    denied = PermissionError(13, "denied")
    with (
        patch.object(cloud.token_store, "load", side_effect=denied),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=RuntimeError("sdk down"),
        ),
    ):
        cloud.sync({"ops": []})
    assert failures == [str(denied)]


def test_stale_generation_drops_without_capture(app, cloud) -> None:
    _, failures, _ = _wired(cloud)
    denied = PermissionError(13, "denied stale")

    def _stale_load() -> None:
        cloud._state.account_generation += 1
        raise denied

    with (
        patch.object(cloud.token_store, "load", side_effect=_stale_load),
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        cloud.sync({"ops": []})
        _pump_until(app, failures, timeout=3.0)
        app.processEvents()
    assert failures == []
    capture.assert_not_called()


def test_sdk_gate_blocks_delivery_without_tests_opt_in() -> None:
    import sentry_sdk

    reset_cloud_credential_budget()
    with patch.object(sentry_sdk, "capture_exception") as sdk_capture:
        assert capture_cloud_credential("sync", "filesystem") is True
        sdk_capture.assert_not_called()
    reset_cloud_credential_budget()
    with (
        patch.dict(
            os.environ, {"POMODOROUGH_SENTRY_ALLOW_IN_TESTS": "1"}, clear=False
        ),
        patch.object(sentry_sdk, "capture_exception") as sdk_capture,
    ):
        assert capture_cloud_credential("sync", "filesystem") is True
        sdk_capture.assert_called_once()
        assert CANARY_TOKEN not in str(sdk_capture.call_args.args[0])
    os.environ.pop("POMODOROUGH_SENTRY_ALLOW_IN_TESTS", None)
    reset_cloud_credential_budget()
