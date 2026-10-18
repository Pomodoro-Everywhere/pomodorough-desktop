"""R50-D06: close D04 credential-capture gaps (login/legacy/revision/fallback)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.network import CloudService, TokenStore
from pomodorough.secure_store import SecureStoreError, TokenCommitPendingError
from pomodorough.sentry_monitoring import (
    capture_cloud_credential,
    capture_fallback_count,
    reset_capture_fallback_count,
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
    try:
        yield service
    finally:
        service.shutdown()


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    reset_cloud_credential_budget()
    reset_capture_fallback_count()
    try:
        yield
    finally:
        reset_cloud_credential_budget()
        reset_capture_fallback_count()


def _wired(service: CloudService) -> tuple[list, list]:
    statuses: list[str] = []
    failures: list[str] = []
    service.status_changed.connect(statuses.append)
    service.failure.connect(failures.append)
    return statuses, failures


def _infra(message: str) -> SecureStoreError:
    error = SecureStoreError(message)
    error.__cause__ = OSError("keychain down")
    return error


def test_login_preflight_infra_reports_payload_free(cloud) -> None:
    _, failures = _wired(cloud)
    error = _infra(f"vault read {CANARY_TOKEN} at {CANARY_PATH}")
    calls: list[BaseException] = []
    with (
        patch.object(cloud._accounts, "require_authentication_ready", side_effect=error),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda exc: calls.append(exc),
        ),
    ):
        cloud.login()
    assert failures == [str(error)]
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential login failed: secure-store-infra"
    assert CANARY_TOKEN not in str(calls[0])
    assert CANARY_PATH not in str(calls[0])


def test_login_preflight_quiet_stays_silent(cloud) -> None:
    _, failures = _wired(cloud)
    cases = [
        SecureStoreError("Stored OAuth credentials are malformed."),
        TokenCommitPendingError("Secure credential commit is pending; retry."),
    ]
    with (
        patch(
            "pomodorough.sentry_monitoring.capture_exception"
        ) as capture,
    ):
        for error in cases:
            failures.clear()
            with patch.object(
                cloud._accounts, "require_authentication_ready", side_effect=error
            ):
                cloud.login()
            assert failures == [str(error)]
        capture.assert_not_called()


def test_revision_stream_preflight_reports_and_quiet(cloud) -> None:
    _, failures = _wired(cloud)
    cloud._state.authenticated = True
    cloud._state.access_token = "tok"
    infra = _infra(f"vault {CANARY_TOKEN}")
    calls: list[BaseException] = []
    with (
        patch.object(cloud._accounts, "require_authentication_ready", side_effect=infra),
        patch.object(cloud._revisions, "start") as start,
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda exc: calls.append(exc),
        ),
    ):
        cloud.start_revision_stream()
    assert failures == [str(infra)]
    start.assert_not_called()
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential revision_stream failed: secure-store-infra"
    assert CANARY_TOKEN not in str(calls[0])


def test_revision_stream_malformed_quiet(cloud) -> None:
    _, failures = _wired(cloud)
    cloud._state.authenticated = True
    cloud._state.access_token = "tok"
    malformed = SecureStoreError("Stored OAuth credentials are malformed.")
    with (
        patch.object(
            cloud._accounts, "require_authentication_ready", side_effect=malformed
        ),
        patch.object(cloud._revisions, "start") as start,
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.start_revision_stream()
    assert failures == [str(malformed)]
    start.assert_not_called()
    capture.assert_not_called()


def test_legacy_save_oserror_captures_bounded_payload_free(tmp_path) -> None:
    store = TokenStore(
        "device-7", secret_store=None, fallback_path=tmp_path / "session.json"
    )
    encoded = f'{{"refreshToken":"{CANARY_TOKEN}"}}'
    calls: list[BaseException] = []
    with (
        patch("pomodorough.network.shutil.which", return_value="/usr/bin/secret-tool"),
        patch(
            "pomodorough.network.subprocess.run",
            side_effect=OSError(f"spawn {CANARY_PATH}"),
        ),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=lambda exc: calls.append(exc),
        ),
    ):
        assert store._save_legacy_secret_durable_locked(encoded) is False
        assert store._save_legacy_secret_durable_locked(encoded) is False
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential save failed: filesystem"
    assert CANARY_TOKEN not in str(calls[0])
    assert CANARY_PATH not in str(calls[0])
    assert (tmp_path / "session.json").read_text(encoding="utf-8") == encoded


def test_capture_cloud_credential_counts_sdk_fallback() -> None:
    before = capture_fallback_count()
    with patch(
        "pomodorough.sentry_monitoring.capture_exception",
        side_effect=RuntimeError("sdk down"),
    ):
        assert capture_cloud_credential("sync", "filesystem") is True
    assert capture_fallback_count() == before + 1
    assert capture_cloud_credential("sync", "filesystem") is False
    assert capture_fallback_count() == before + 1
