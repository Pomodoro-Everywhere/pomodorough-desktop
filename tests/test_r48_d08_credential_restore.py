"""R48-D08: credential-restore PermissionError must report once to Sentry."""

from __future__ import annotations

import os
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.secure_store import SecureStoreError
from pomodorough.sentry_monitoring import reset_cloud_credential_budget


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


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


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    reset_cloud_credential_budget()
    try:
        yield
    finally:
        reset_cloud_credential_budget()


@pytest.fixture()
def cloud(tmp_path: Path) -> Iterator[CloudService]:
    service = CloudService(
        "device-1",
        "https://example.test",
        token_store=TokenStore(
            "device-1",
            secret_store=_MemorySecretStore(),
            fallback_path=tmp_path / "session.json",
        ),
    )
    try:
        yield service
    finally:
        service.shutdown()


def _wired(cloud: CloudService):
    statuses: list[str] = []
    failures: list[str] = []
    expired: list[bool] = []
    cloud.status_changed.connect(statuses.append)
    cloud.failure.connect(failures.append)
    cloud.session_expired.connect(lambda: expired.append(True))
    return statuses, failures, expired


def _offline_error() -> ApiError:
    error = ApiError("Could not reach Pomodorough")
    error.__cause__ = urllib.error.URLError("offline")
    return error


def _infra_secure_store_error() -> SecureStoreError:
    error = SecureStoreError("Secure value could not be read")
    error.__cause__ = OSError("keychain read failed")
    return error


def test_real_worker_permission_error_reports_once(app, cloud) -> None:
    statuses, failures, _ = _wired(cloud)
    denied = PermissionError(13, "Permission denied")
    with (
        patch.object(cloud.token_store, "load", side_effect=denied),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
        deadline = time.monotonic() + 10
        while not failures and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    assert failures == [str(denied)]
    assert statuses[-1] == "OFFLINE • RETRYING"
    assert not cloud.authenticated
    assert cloud._state.access_token is None
    capture.assert_called_once()
    assert isinstance(capture.call_args.args[0], RuntimeError)
    assert str(capture.call_args.args[0]) == "cloud credential restore failed: filesystem"


def test_permission_error_reports_once_with_offline_notice(cloud) -> None:
    statuses, failures, _ = _wired(cloud)
    denied = PermissionError(13, "Permission denied")
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", side_effect=denied),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert statuses == ["CONNECTING", "OFFLINE • RETRYING"]
    assert failures == [str(denied)]
    assert not cloud.authenticated
    capture.assert_called_once()
    assert isinstance(capture.call_args.args[0], RuntimeError)
    assert str(capture.call_args.args[0]) == "cloud credential restore failed: filesystem"


def test_infra_backed_secure_store_error_reports_once(cloud) -> None:
    _, failures, _ = _wired(cloud)
    error = _infra_secure_store_error()
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", side_effect=error),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert failures == [str(error)]
    capture.assert_called_once()
    assert isinstance(capture.call_args.args[0], RuntimeError)
    assert str(capture.call_args.args[0]) == "cloud credential restore failed: secure-store-infra"


def test_malformed_secure_store_error_stays_silent(cloud) -> None:
    _, failures, _ = _wired(cloud)
    error = SecureStoreError("Stored OAuth credentials are malformed.")
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", side_effect=error),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert failures == [str(error)]
    capture.assert_not_called()


def test_offline_api_error_stays_silent(cloud) -> None:
    statuses, failures, _ = _wired(cloud)
    error = _offline_error()
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
        patch.object(cloud, "_ensure_access", side_effect=error),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert statuses[-1] == "OFFLINE • RETRYING"
    assert failures == [str(error)]
    capture.assert_not_called()


def test_401_expires_session_silently(cloud) -> None:
    statuses, failures, expired = _wired(cloud)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
        patch.object(cloud, "_ensure_access", side_effect=ApiError("expired", 401)),
        patch.object(cloud.token_store, "clear") as clear,
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert expired == [True]
    assert failures == []
    assert statuses[-1] == "SESSION EXPIRED • SIGN IN AGAIN"
    clear.assert_called_once_with()
    capture.assert_not_called()


def test_cancel_stays_silent(cloud) -> None:
    _, failures, _ = _wired(cloud)
    error = ApiError(cloud.strings.text("network.error.sign_in_cancelled"))
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
        patch.object(cloud, "_ensure_access", side_effect=error),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert failures == [str(error)]
    assert not cloud.authenticated
    capture.assert_not_called()


def test_malformed_profile_response_stays_silent(cloud) -> None:
    _, failures, _ = _wired(cloud)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
        patch.object(cloud, "_ensure_access", return_value="access"),
        patch.object(cloud, "_request", return_value={"unexpected": 1}),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert len(failures) == 1
    assert not cloud.authenticated
    capture.assert_not_called()


def test_unexpected_error_reports_once(cloud) -> None:
    _, failures, _ = _wired(cloud)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", side_effect=RuntimeError("boom")),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
    assert failures == ["boom"]
    capture.assert_called_once()
    assert isinstance(capture.call_args.args[0], RuntimeError)
    assert str(capture.call_args.args[0]) == "cloud credential restore failed: unexpected"
