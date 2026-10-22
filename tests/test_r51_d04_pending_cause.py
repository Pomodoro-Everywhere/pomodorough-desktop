"""R51-D04: pending-commit wrapping must not silence provider faults.

Classify the underlying cause of TokenCommitPendingError with fixed
payload-free diagnostics while retaining pending state and quiet
plain/offline pending. Uses real TokenStore commit paths and public
CloudService workers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
from pomodorough.secure_store import (
    PlatformSecretStore,
    SecureStoreError,
    TokenCommitPendingError,
    UncertainSecretStoreError,
)
from pomodorough.sentry_monitoring import (
    reset_cloud_credential_budget,
)
from test_secure_store import linux_secret_store

CANARY_TOKEN = "canary-refresh-TOKEN-abc123"
CANARY_PATH = "/tmp/canary-secret-path/session.json"
API = "https://example.test"
DEVICE = "device-r51d04"


class _FailingStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail = False

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if self.fail:
            self.values[key] = value
            cause = subprocess.TimeoutExpired(["secret-tool", "store"], 15)
            raise UncertainSecretStoreError("Platform secure storage timed out") from cause
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _tokens(name: str) -> dict[str, str]:
    base = datetime(2099, 2, 3, 4, 5, 6, tzinfo=UTC)
    return {
        "accessToken": f"{name}-access-{CANARY_TOKEN}",
        "accessTokenExpiresAt": base.isoformat().replace("+00:00", "Z"),
        "refreshToken": f"{name}-refresh-{CANARY_TOKEN}",
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
def setup(tmp_path: Path):
    store = _FailingStore()
    token_store = TokenStore(DEVICE, secret_store=store, fallback_path=tmp_path / "s.json")
    token_store.bind_api(API)
    service = CloudService(DEVICE, API, token_store=token_store)
    service._state.authenticated = True
    service._state.access_token = "old-access"
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    try:
        yield service, store, tmp_path
    finally:
        service.shutdown()


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    reset_cloud_credential_budget()
    try:
        yield
    finally:
        reset_cloud_credential_budget()


def _fake_request(seen: list) -> object:
    def _request(method: str, url: str, payload=None, **kwargs):
        seen.append(url)
        if url.endswith("/auth/refresh"):
            return dict(_tokens("new"))
        if url.endswith("/api/v1/sync"):
            return {"ok": True}
        if url.endswith("/api/v1/bootstrap"):
            return {"mode": "none"}
        if url.endswith("/api/v1/bootstrap/resolve"):
            return {"ok": True}
        if url.endswith("/api/v1/me"):
            return {"user": {"id": "user-1"}}
        raise AssertionError(f"unexpected reach: {url}")

    return _request


def _wired(service: CloudService) -> tuple[list, list]:
    failures: list[str] = []
    service.failure.connect(failures.append)
    return failures, []


def test_cause_unwraps_pending_infra() -> None:
    timeout = subprocess.TimeoutExpired(["secret-tool", "store"], 15)
    uncertain = UncertainSecretStoreError("timed out")
    uncertain.__cause__ = timeout
    pending = TokenCommitPendingError("pending")
    pending.__cause__ = uncertain
    assert _cloud_credential_cause(pending) == "secure-store-infra"
    direct = TokenCommitPendingError("pending")
    direct.__cause__ = TimeoutError("callback timed out")
    assert _cloud_credential_cause(direct) == "filesystem"
    assert _cloud_credential_cause(TokenCommitPendingError("plain")) is None


def test_cause_quiet_for_malformed_offline_wrapped() -> None:
    malformed = SecureStoreError("Stored OAuth credentials are malformed.")
    wrapped = TokenCommitPendingError("pending")
    wrapped.__cause__ = malformed
    assert _cloud_credential_cause(wrapped) is None
    offline = ApiError("Could not reach Pomodorough")
    wrapped_offline = TokenCommitPendingError("pending")
    wrapped_offline.__cause__ = offline
    assert _cloud_credential_cause(wrapped_offline) is None
    assert _cloud_credential_cause(ValueError("bad")) is None


@pytest.mark.parametrize("operation", ["sync", "preview_bootstrap", "resolve_bootstrap"])
def test_workers_report_timeout_pending(app, setup, operation) -> None:
    service, store, _ = setup
    failures, _ = _wired(service)
    store.fail = False
    service._session.accept_tokens(_tokens("old"))
    assert not service.token_store._credential_commit_path().exists()
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.fail = True
    seen: list[str] = []
    calls: list[BaseException] = []
    with (
        patch.object(service, "_request", side_effect=_fake_request(seen)),
        patch.object(service._session, "request", side_effect=_fake_request(seen)),
        patch.object(service, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
    ):
        if operation == "sync":
            service.sync({"ops": []})
        elif operation == "resolve_bootstrap":
            service.resolve_bootstrap({"choice": "keep"})
        else:
            service.preview_bootstrap()
        service._state.busy = False
        service._state.sync_queued = None
    assert len(failures) == 1
    assert len(calls) == 1
    text = str(calls[0])
    assert text.endswith("secure-store-infra")
    assert CANARY_TOKEN not in text and CANARY_PATH not in text
    assert service.token_store._credential_commit_path().exists()
    assert service._state.refresh_token.endswith(CANARY_TOKEN)
    failures.clear()
    store.fail = False


def test_failed_replay_reports_and_recovers(app, setup) -> None:
    service, store, tmp_path = setup
    failures, _ = _wired(service)
    store.fail = False
    service._session.accept_tokens(_tokens("old"))
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.fail = True
    seen: list[str] = []
    calls: list[BaseException] = []
    with (
        patch.object(service, "_request", side_effect=_fake_request(seen)),
        patch.object(service._session, "request", side_effect=_fake_request(seen)),
        patch.object(service, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
    ):
        service.sync({"ops": []})
        service._state.busy = False
        service._state.sync_queued = None
        assert len(failures) == 1 and len(calls) == 1
        journal = service.token_store._credential_commit_path()
        assert journal.exists()
        doc = json.loads(journal.read_text())
        assert doc["stored"]["refreshToken"].endswith(CANARY_TOKEN)
        provider = json.loads(store.values[f"oauth:{DEVICE}"].decode())
        assert provider["refreshToken"].endswith(CANARY_TOKEN)
        service.sync({"ops": []})
        service._state.busy = False
        service._state.sync_queued = None
        assert len(failures) == 2 and len(calls) == 1
        store.fail = False
        service.sync({"ops": []})
        assert not journal.exists()
        assert service.token_store.load()["refreshToken"].endswith(CANARY_TOKEN)
    failures.clear()


def test_restore_refresh_reports_timeout(app, setup) -> None:
    service, store, _ = setup
    failures, _ = _wired(service)
    store.fail = False
    service._session.accept_tokens(_tokens("old"))
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.fail = True
    calls: list[BaseException] = []
    with (
        patch.object(service, "_request", side_effect=_fake_request([])),
        patch.object(service._session, "request", side_effect=_fake_request([])),
        patch.object(service, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception",
              side_effect=calls.append),
    ):
        service.restore()
        service._state.busy = False
    assert len(failures) == 1 and len(calls) == 1
    assert str(calls[0]) == "cloud credential restore failed: secure-store-infra"
    assert CANARY_TOKEN not in str(calls[0])
    failures.clear()
    store.fail = False


def test_real_worker_sync_reports_once(app, tmp_path: Path) -> None:
    store = _FailingStore()
    token_store = TokenStore(DEVICE, secret_store=store,
                             fallback_path=tmp_path / "real.json")
    token_store.bind_api(API)
    service = CloudService(DEVICE, API, token_store=token_store)
    service._sign_out_cleanup_timer.stop()
    service._revocation_restore_timer.stop()
    service._state.authenticated = True
    service._state.access_token = "old-access"
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    failures: list[str] = []
    service.failure.connect(failures.append)
    try:
        service._session.accept_tokens(_tokens("old"))
        service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
        store.fail = True
        seen: list[str] = []
        calls: list[BaseException] = []
        with (
            patch.object(service, "_request", side_effect=_fake_request(seen)),
            patch.object(service._session, "request", side_effect=_fake_request(seen)),
            patch("pomodorough.sentry_monitoring.capture_exception",
                  side_effect=calls.append),
        ):
            service.sync({"ops": []})
            deadline = time.monotonic() + 10.0
            while not failures and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
        assert len(failures) == 1 and len(calls) == 1
        assert service.token_store._credential_commit_path().exists()
    finally:
        service.shutdown()


def test_plain_pending_and_offline_stay_quiet(app, setup) -> None:
    service, _, _ = setup
    failures, _ = _wired(service)
    plain = TokenCommitPendingError("Secure credential commit is pending; retry.")
    offline = ApiError("Could not reach Pomodorough")
    with (
        patch.object(service, "_start", side_effect=_run_immediately),
        patch("pomodorough.sentry_monitoring.capture_exception") as capture,
    ):
        for error in (plain, offline):
            failures.clear()
            service._state.busy = False
            service._state.sync_queued = None
            service._state.authenticated = True
            with patch.object(service.token_store, "load", side_effect=error):
                service.sync({"ops": []})
            assert failures == [str(error)]
        capture.assert_not_called()


def test_sdk_envelope_payload_free(app, setup) -> None:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    from pomodorough import sentry_monitoring as monitoring

    events: list[dict] = []

    class MemoryTransport(Transport):
        def capture_envelope(self, envelope) -> None:
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    service, store, _ = setup
    failures, _ = _wired(service)
    store.fail = False
    service._session.accept_tokens(_tokens("old"))
    service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
    store.fail = True
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
        patch.object(service, "_request", side_effect=_fake_request([])),
        patch.object(service._session, "request", side_effect=_fake_request([])),
        patch.object(service, "_start", side_effect=_run_immediately),
    ):
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            service.sync({"ops": []})
            service._state.busy = False
            service._state.sync_queued = None
    client.close()
    assert len(failures) == 1 and len(events) == 1
    encoded = json.dumps(events)
    assert CANARY_TOKEN not in encoded and CANARY_PATH not in encoded
    assert events[0]["exception"]["values"][0]["type"] == "RuntimeError"
    failures.clear()
    store.fail = False


_R51D04_TOOL = """#!{python}
import json, os, sys


def key_of(args):
    for position, token in enumerate(args):
        if token == "key" and position + 1 < len(args):
            return args[position + 1]
    raise SystemExit(3)


database = os.environ["R51D04_DB"]
try:
    with open(database) as handle:
        items = json.load(handle)
except FileNotFoundError:
    items = {{}}
operation, args = sys.argv[1], sys.argv[2:]
if operation == "lookup":
    value = items.get(key_of(args))
    if value is None:
        raise SystemExit(1)
    sys.stdout.write(value + "\\n")
elif operation == "store":
    items[key_of(args)] = sys.stdin.read().strip()
    with open(database, "w") as handle:
        handle.write(json.dumps(items))
        handle.flush()
        os.fsync(handle.fileno())
    if os.environ.get("R51D04_FAIL_AFTER_WRITE") == "1":
        sys.stderr.write("lost acknowledgement after durable write\\n")
        raise SystemExit(2)
elif operation == "clear":
    items.pop(key_of(args), None)
    with open(database, "w") as handle:
        handle.write(json.dumps(items))
        handle.flush()
        os.fsync(handle.fileno())
else:
    raise SystemExit(3)
"""


def _install_r51d04_tool(root: Path) -> Path:
    binary = root / "bin" / "secret-tool"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(_R51D04_TOOL.format(python=sys.executable))
    binary.chmod(0o700)
    return binary.parent


@pytest.mark.skipif(os.name == "nt", reason="Synthetic secret-tool requires POSIX")
def test_real_adapter_write_then_error_reports_pending(app, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("R51D04_DB", str(tmp_path / "provider.json"))
    monkeypatch.setenv(
        "PATH", str(_install_r51d04_tool(tmp_path)) + os.pathsep + os.environ["PATH"])
    with linux_secret_store(tmp_path):
        token_store = TokenStore("r51d04-real", fallback_path=tmp_path / "real.json")
        assert isinstance(token_store.secret_store, PlatformSecretStore)
        token_store.bind_api(API)
        service = CloudService("r51d04-real", API, token_store=token_store)
        try:
            service._state.authenticated = True
            service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
            failures, _ = _wired(service)
            service._session.accept_tokens(_tokens("old"))
            service._state.access_expires_at = datetime.now(UTC) - timedelta(hours=1)
            monkeypatch.setenv("R51D04_FAIL_AFTER_WRITE", "1")
            calls: list[BaseException] = []
            patched = (
                patch.object(service, "_request", side_effect=_fake_request([])),
                patch.object(service._session, "request", side_effect=_fake_request([])),
                patch.object(service, "_start", side_effect=_run_immediately),
                patch("pomodorough.sentry_monitoring.capture_exception",
                      side_effect=calls.append),
            )
            with patched[0], patched[1], patched[2], patched[3]:
                service.sync({"ops": []})
                service._state.busy = False
                service._state.sync_queued = None
            assert len(failures) == 1 and len(calls) == 1
            assert str(calls[0]).endswith("secure-store-infra")
            assert CANARY_TOKEN not in str(calls[0])
            assert token_store._credential_commit_path().exists()
            monkeypatch.delenv("R51D04_FAIL_AFTER_WRITE")
            with patched[0], patched[1], patched[2], patched[3]:
                service.sync({"ops": []})
                service._state.busy = False
                service._state.sync_queued = None
            assert not token_store._credential_commit_path().exists()
        finally:
            service.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="Synthetic secret-tool requires POSIX")
def test_real_adapter_prewrite_refusal_aborts_without_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("R51D04_DB", str(tmp_path / "provider.json"))
    monkeypatch.setenv(
        "PATH", str(_install_r51d04_tool(tmp_path)) + os.pathsep + os.environ["PATH"])
    with linux_secret_store(tmp_path):
        store = TokenStore("r51d04-pre", fallback_path=tmp_path / "pre.json")
        assert isinstance(store.secret_store, PlatformSecretStore)
        store.bind_api(API)
        store.save(_tokens("old"))
        assert not store._credential_commit_path().exists()
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(SecureStoreError) as failure:
            store.save(_tokens("new"))
        assert not isinstance(failure.value, TokenCommitPendingError)
        assert "secret-tool was not found" in str(failure.value)
        assert not store._credential_commit_path().exists()
        assert _cloud_credential_cause(failure.value) == "secure-store-infra"
        monkeypatch.setenv(
            "PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
        assert store.load()["refreshToken"].endswith(CANARY_TOKEN)
