"""R51-L01: alternating credential operations share a bounded budget.

Same-key repeats stay suppressed, alternating keys keep separate
timestamps without rearming each other, a shared epoch cap bounds
many-key bursts, restore uses the same budgeted payload-free path,
and success rearms recurrence. Uses real worker callbacks and one
SDK-envelope case through production scrubbing.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough import sentry_monitoring as monitoring
from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.secure_store import SecureStoreError

CANARY_TOKEN = "canary-refresh-TOKEN-abc123"
CANARY_PATH = "/tmp/canary-secret-path/session.json"
DEVICE = "device-r51l01"
API = "https://example.test"
FROZEN_NOW = 100.0


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _denied() -> PermissionError:
    return PermissionError(13, f"denied {CANARY_TOKEN} at {CANARY_PATH}")


def _infra() -> SecureStoreError:
    error = SecureStoreError(f"vault read {CANARY_TOKEN} at {CANARY_PATH}")
    error.__cause__ = OSError("keychain down")
    return error


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
        DEVICE,
        API,
        token_store=TokenStore(
            DEVICE,
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
    monitoring.reset_cloud_credential_budget()
    monitoring.reset_capture_fallback_count()
    try:
        yield
    finally:
        monitoring.reset_cloud_credential_budget()
        monitoring.reset_capture_fallback_count()


def _wired(service: CloudService) -> list[str]:
    failures: list[str] = []
    service.failure.connect(failures.append)
    return failures


def _pump_until(app: QApplication, done: list, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not done and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def _assert_payload_free(calls: list[BaseException]) -> None:
    for captured in calls:
        text = str(captured)
        assert isinstance(captured, RuntimeError)
        assert CANARY_TOKEN not in text
        assert CANARY_PATH not in text
        assert DEVICE not in text
        assert text.startswith("cloud credential ")
        assert text.endswith(("filesystem", "secure-store-infra", "unexpected"))


def test_alternating_sync_preview_bounded_per_key(cloud) -> None:
    failures = _wired(cloud)
    calls: list[BaseException] = []
    with (
        patch.object(cloud.token_store, "load", side_effect=_denied()),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        for index in range(8):
            if index % 2:
                cloud.preview_bootstrap()
            else:
                cloud.sync({"ops": []})
    assert len(failures) == 8
    assert len(calls) == 2
    assert {str(call) for call in calls} == {
        "cloud credential sync failed: filesystem",
        "cloud credential preview_bootstrap failed: filesystem",
    }
    _assert_payload_free(calls)
    assert not cloud._state.busy


def test_login_revision_login_bounded(cloud) -> None:
    failures = _wired(cloud)
    cloud._state.authenticated = True
    calls: list[BaseException] = []
    with (
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        with patch.object(cloud, "_authorize_google", side_effect=_denied()):
            with patch.object(cloud, "_start", side_effect=_run_immediately):
                cloud.login()
        with patch.object(
            cloud._accounts, "require_authentication_ready", side_effect=_infra()
        ):
            cloud.start_revision_stream()
        with patch.object(cloud, "_authorize_google", side_effect=_denied()):
            with patch.object(cloud, "_start", side_effect=_run_immediately):
                cloud.login()
    assert len(failures) == 3
    assert len(calls) == 2
    assert {str(call) for call in calls} == {
        "cloud credential login failed: filesystem",
        "cloud credential revision_stream failed: secure-store-infra",
    }
    _assert_payload_free(calls)


def test_restore_capped_and_payload_free(cloud) -> None:
    failures = _wired(cloud)
    calls: list[BaseException] = []
    with (
        patch.object(cloud.token_store, "load", side_effect=_denied()),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        for _ in range(8):
            cloud.restore()
    assert len(failures) == 8
    assert len(calls) == 1
    assert str(calls[0]) == "cloud credential restore failed: filesystem"
    _assert_payload_free(calls)


def test_success_then_fault_same_key_recurs(cloud) -> None:
    failures = _wired(cloud)
    calls: list[BaseException] = []
    with (
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud, "start_revision_stream"),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        with patch.object(cloud.token_store, "load", side_effect=_denied()):
            cloud.sync({"ops": []})
        with patch.object(cloud, "_authorized_request", return_value={"ok": True}):
            cloud.sync({"ops": []})
        with patch.object(cloud.token_store, "load", side_effect=_denied()):
            cloud.sync({"ops": []})
    assert len(failures) == 2
    assert len(calls) == 2
    assert str(calls[0]) == str(calls[1]) == "cloud credential sync failed: filesystem"
    _assert_payload_free(calls)


def test_restore_success_rearms_budget(cloud) -> None:
    failures = _wired(cloud)
    calls: list[BaseException] = []
    with (
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud, "start_revision_stream"),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        with patch.object(cloud.token_store, "load", side_effect=_denied()):
            cloud.restore()
        with (
            patch.object(cloud.token_store, "load", return_value={"present": True}),
            patch.object(cloud, "_ensure_access", return_value="tok"),
            patch.object(cloud, "_request", return_value={"user": {"id": "u1"}}),
        ):
            cloud.restore()
        with patch.object(cloud.token_store, "load", side_effect=_denied()):
            cloud.restore()
    assert len(failures) == 2
    assert len(calls) == 2
    assert str(calls[0]) == str(calls[1]) == "cloud credential restore failed: filesystem"
    _assert_payload_free(calls)


def test_shared_burst_capped_at_budget() -> None:
    calls: list[BaseException] = []
    keys = [
        ("sync", "filesystem"),
        ("preview_bootstrap", "filesystem"),
        ("login", "filesystem"),
        ("restore", "filesystem"),
        ("sync", "unexpected"),
        ("login", "unexpected"),
    ]
    with (
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        for operation, cause in keys:
            monitoring.capture_cloud_credential(operation, cause)
        assert len(calls) == monitoring.CLOUD_CREDENTIAL_SHARED_BUDGET
        assert monitoring.capture_cloud_credential("sync", "raw boom") is False
        assert monitoring.capture_cloud_credential("evil_op", "filesystem") is False
        assert len(calls) == monitoring.CLOUD_CREDENTIAL_SHARED_BUDGET
    _assert_payload_free(calls)


def test_sdk_envelopes_bounded_and_payload_free(cloud) -> None:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    events: list[dict] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope) -> None:
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    failures = _wired(cloud)
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        transport=MemoryTransport,
        default_integrations=False,
        include_local_variables=False,
        send_client_reports=False,
        before_send=monitoring.scrub_sentry_event,
    )
    with (
        patch.dict(os.environ, {"POMODOROUGH_SENTRY_ALLOW_IN_TESTS": "1"}),
        patch.object(cloud.token_store, "load", side_effect=_denied()),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            for index in range(8):
                if index % 2:
                    cloud.preview_bootstrap()
                else:
                    cloud.sync({"ops": []})
    client.close()
    assert len(failures) == 8
    assert len(events) == 2
    encoded = json.dumps(events)
    assert CANARY_TOKEN not in encoded
    assert CANARY_PATH not in encoded


def test_restore_stale_silent(app, cloud) -> None:
    failures = _wired(cloud)

    def _stale_load() -> None:
        cloud._state.account_generation += 1
        raise _denied()

    with (
        patch.object(cloud.token_store, "load", side_effect=_stale_load),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        cloud.restore()
        _pump_until(app, failures, timeout=3.0)
        app.processEvents()
    assert failures == []
    capture.assert_not_called()


def test_reporter_failure_isolated(cloud) -> None:
    failures = _wired(cloud)
    before = monitoring.capture_fallback_count()
    with (
        patch.object(cloud.token_store, "load", side_effect=_denied()),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
            side_effect=RuntimeError("sdk down"),
        ),
        patch.object(monitoring.time, "monotonic", return_value=FROZEN_NOW),
    ):
        for index in range(8):
            if index % 2:
                cloud.preview_bootstrap()
            else:
                cloud.sync({"ops": []})
    assert len(failures) == 8
    assert monitoring.capture_fallback_count() == before + 2


def test_expected_restore_errors_stay_silent(cloud) -> None:
    failures = _wired(cloud)
    cases = [
        ApiError("offline"),
        ApiError("expired", 401),
        ValueError("bad input"),
        KeyError("missing"),
    ]
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        for error in cases:
            failures.clear()
            with patch.object(cloud.token_store, "load", side_effect=error):
                cloud.restore()
            if isinstance(error, ApiError) and error.status == 401:
                assert failures == []
            else:
                assert failures == [str(error)]
    capture.assert_not_called()


def test_clock_advance_rearms_throttle() -> None:
    monitoring.reset_cloud_credential_budget()
    try:
        interval = monitoring.CLOUD_CREDENTIAL_REPORT_INTERVAL_SECONDS
        assert monitoring.should_capture_cloud_credential(
            "sync", "filesystem", now_monotonic=FROZEN_NOW) is True
        assert monitoring.should_capture_cloud_credential(
            "sync", "filesystem", now_monotonic=FROZEN_NOW) is False
        assert monitoring.should_capture_cloud_credential(
            "sync", "filesystem",
            now_monotonic=FROZEN_NOW + interval - 0.1) is False
        later = FROZEN_NOW + interval + 0.1
        assert monitoring.should_capture_cloud_credential(
            "sync", "filesystem", now_monotonic=later) is True
        assert monitoring.should_capture_cloud_credential(
            "restore", "filesystem", now_monotonic=later) is True
    finally:
        monitoring.reset_cloud_credential_budget()


def test_clock_advance_reports_again_end_to_end(cloud) -> None:
    failures = _wired(cloud)
    calls: list[BaseException] = []
    interval = monitoring.CLOUD_CREDENTIAL_REPORT_INTERVAL_SECONDS
    times = [FROZEN_NOW, FROZEN_NOW, FROZEN_NOW + interval + 0.1]
    with (
        patch.object(cloud.token_store, "load", side_effect=_denied()),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
        patch.object(monitoring.time, "monotonic", side_effect=list(times)),
    ):
        for _ in range(3):
            cloud.sync({"ops": []})
    assert len(failures) == 3
    assert len(calls) == 2
    assert str(calls[0]) == str(calls[1]) == "cloud credential sync failed: filesystem"
    _assert_payload_free(calls)
