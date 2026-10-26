"""R52-D03: offline startup restore retries boundedly when connectivity returns."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pomodorough.network import ApiError, CloudService, TokenStore
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


def _offline_error() -> ApiError:
    return ApiError("Could not reach Pomodorough")


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


def _wired(service: CloudService):
    statuses: list[str] = []
    failures: list[str] = []
    users: list[Any] = []
    service.status_changed.connect(statuses.append)
    service.failure.connect(failures.append)
    service.signed_in.connect(users.append)
    return statuses, failures, users


def test_offline_restore_recovers_on_retry(cloud) -> None:
    statuses, failures, users = _wired(cloud)
    user = {"id": "user-1"}
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(
            cloud,
            "_ensure_access",
            side_effect=[_offline_error(), "access"],
        ) as ensure,
        patch.object(cloud, "_request", return_value={"user": user}),
        patch.object(cloud, "start_revision_stream") as stream,
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        assert failures == [str(_offline_error())]
        assert [ms for ms, _ in scheduled_calls] == [1000]
        scheduled_calls[0][1]()
    assert cloud.authenticated
    assert users == [user]
    assert ensure.call_count == 2
    assert cloud._restore_attempt == 0
    assert failures == [str(_offline_error())]
    assert statuses == ["CONNECTING", "OFFLINE • RETRYING", "CONNECTING", "SYNC READY"]
    stream.assert_called_once_with()


def test_retries_stop_after_bound(cloud) -> None:
    _, failures, _ = _wired(cloud)
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=_offline_error()) as ensure,
        patch.object(cloud, "start_revision_stream"),
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        while len(scheduled_calls) < 6:
            scheduled_calls[-1][1]()
        count_before = ensure.call_count
        scheduled_calls[-1][1]()
    assert [ms for ms, _ in scheduled_calls] == [1000, 2000, 4000, 8000, 16000, 30000]
    assert ensure.call_count == count_before + 1 == 7
    assert failures == [str(_offline_error())]
    assert not cloud.authenticated


def test_retry_fenced_by_generation(cloud) -> None:
    _, _, _ = _wired(cloud)
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=_offline_error()) as ensure,
        patch.object(cloud, "start_revision_stream"),
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        cloud._state.account_generation += 1
        scheduled_calls[0][1]()
    assert ensure.call_count == 1
    assert not cloud.authenticated


def test_retry_fenced_by_route(cloud) -> None:
    _, _, _ = _wired(cloud)
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=_offline_error()) as ensure,
        patch.object(cloud, "start_revision_stream"),
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        cloud.api_base = "https://other.test"
        scheduled_calls[0][1]()
    assert ensure.call_count == 1
    assert not cloud.authenticated


def test_retry_skipped_after_shutdown(cloud) -> None:
    _, _, _ = _wired(cloud)
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=_offline_error()) as ensure,
        patch.object(cloud, "start_revision_stream"),
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        cloud.shutdown()
        scheduled_calls[0][1]()
    assert ensure.call_count == 1


def test_expiry_schedules_no_retry(cloud) -> None:
    statuses, failures, _ = _wired(cloud)
    expired_events: list[bool] = []
    cloud.session_expired.connect(lambda: expired_events.append(True))
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=ApiError("expired", 401)),
        patch.object(cloud, "start_revision_stream"),
        patch("pomodorough.network.QTimer.singleShot") as scheduled,
    ):
        cloud.restore()
    scheduled.assert_not_called()
    assert expired_events == [True]
    assert failures == []
    assert statuses[-1] == "SESSION EXPIRED • SIGN IN AGAIN"


def test_missing_session_schedules_no_retry(cloud) -> None:
    statuses, _, _ = _wired(cloud)
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud.token_store, "load", return_value=None),
        patch("pomodorough.network.QTimer.singleShot") as scheduled,
    ):
        cloud.restore()
    scheduled.assert_not_called()
    assert statuses == ["CONNECTING", "LOCAL • SIGN IN TO SYNC"]


def test_manual_restore_invalidates_pending_retry(cloud) -> None:
    _, _, _ = _wired(cloud)
    scheduled_calls: list[tuple[int, Any]] = []
    with (
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(
            cloud.token_store, "load", return_value={"refreshToken": "stored"}
        ),
        patch.object(cloud, "_ensure_access", side_effect=_offline_error()) as ensure,
        patch.object(cloud, "start_revision_stream"),
        patch(
            "pomodorough.network.QTimer.singleShot",
            side_effect=lambda ms, cb: scheduled_calls.append((ms, cb)),
        ),
    ):
        cloud.restore()
        stale = scheduled_calls[0][1]
        cloud.restore()
        stale()
        assert ensure.call_count == 2
        scheduled_calls[-1][1]()
        assert ensure.call_count == 3
