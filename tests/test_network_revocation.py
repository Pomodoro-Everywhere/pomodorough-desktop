from __future__ import annotations

import base64
import io
import json
import subprocess
import time
import unittest
import urllib.error
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_secure_store import linux_secret_store

from pomodorough.network import ApiError, CloudService, TokenStore, _request
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError


def token_response(session: str) -> dict[str, str]:
    return {
        "accessToken": f"{session}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{session}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


class PlatformKeyring:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[list[str], str | None]] = []
        self.reject = lambda _command, _input: False

    def run(self, command: list[str], *, input_text: str | None = None):
        self.calls.append((command, input_text))
        if self.reject(command, input_text):
            return subprocess.CompletedProcess(command, 2, "", "keyring unavailable")
        operation, key = command[1], command[-1]
        if operation == "store":
            self.values[key] = input_text
        elif operation == "clear":
            self.values.pop(key, None)
        elif key not in self.values:
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, self.values.get(key, ""), "")


class DurableRevocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.keyring = PlatformKeyring()
        self.workers = deque()
        self.retries = deque()
        self.request = Mock(return_value={})
        self.enterContext(linux_secret_store(self.root))
        self.enterContext(patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"))
        self.enterContext(patch.object(PlatformSecretStore, "_run", side_effect=self.keyring.run))
        self.enterContext(patch.object(QThreadPool.globalInstance(), "start", side_effect=self.workers.append))
        self.enterContext(patch.object(QTimer, "singleShot", side_effect=lambda delay, callback: self.retries.append((delay, callback))))

    def cloud(self, *, request=None, device="device-1", api="https://example.test"):
        secrets = PlatformSecretStore(self.root / "secrets", kind="oauth")
        tokens = TokenStore(device, secrets, self.root / f"{device}-session.json")
        cloud = CloudService(device, api, token_store=tokens, request=request or self.request)
        self.addCleanup(cloud.shutdown)
        return cloud

    def sign_in(self, cloud: CloudService, session: str) -> None:
        cloud._accept_tokens(token_response(session))
        cloud.authenticated = True

    def run_worker(self) -> None:
        self.workers.popleft().run()

    def restore_revocations(self, cloud: CloudService) -> None:
        self.assertTrue(cloud._revocation_restore_timer.isActive())
        self.assertEqual(cloud._revocation_restore_timer.interval(), 0)
        cloud._revocation_restore_timer.stop()
        cloud._revocation_restore_timer.timeout.emit()
        self.run_worker()

    def pending(self, cloud: CloudService):
        return cloud.token_store.revocations.load(cloud.api_base)

    def test_production_adapters_offline_logout_restart_retry_acknowledged(self) -> None:
        cloud = self.cloud(request=_request)
        self.sign_in(cloud, "offline")
        cleared = []
        cloud.signed_out.connect(lambda: cleared.append(self.pending(cloud)))
        cloud.logout()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            self.run_worker()
        self.assertEqual(len(cleared[0]), 1)
        self.assertFalse(cloud.authenticated)
        self.assertIsNone(cloud.token_store.load())
        self.assertEqual(json.loads(cloud.token_store.fallback_path.read_text()), {"signedOut": True})
        self.assertNotIn(cloud.token_store.secret_key, self.keyring.values)
        cloud.shutdown()
        restarted = self.cloud(request=_request)
        self.restore_revocations(restarted)
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"{}")) as request:
            self.run_worker()
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://example.test/api/v1/auth/logout")
        self.assertEqual(sent.get_header("Authorization"), "Bearer offline-access")
        self.assertEqual(self.pending(restarted), {})
        self.assertFalse(restarted.authenticated)
        self.assertFalse(self.workers)
        self.assertTrue(any("oauth-revocations-v1:" in command[-1] for command, _ in self.keyring.calls))
        for command, _input in self.keyring.calls:
            self.assertNotIn("offline-access", " ".join(command))
            self.assertNotIn("offline-refresh", " ".join(command))
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"offline-access", path.read_bytes())
                self.assertNotIn(b"offline-refresh", path.read_bytes())

    def test_retry_backoff_caps_but_never_exhausts_and_shutdown_stops_callbacks(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "offline")
        self.request.side_effect = ApiError("unreachable", 503)
        cloud.logout()
        delays = []
        for _attempt in range(12):
            self.run_worker()
            delay, retry = self.retries.popleft()
            delays.append(delay)
            retry()
        self.assertEqual(delays, [1000, 2000, 4000, 8000, 16000] + [30000] * 7)
        self.assertEqual(len(self.pending(cloud)), 1)
        self.run_worker()
        cloud.shutdown()
        self.retries.popleft()[1]()
        cloud._restore_revocations()
        self.assertFalse(self.workers)
        self.assertFalse(cloud._revocation_restore_timer.isActive())

    def test_repeated_logout_and_account_switch_preserve_each_session(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "first")
        cloud.logout()
        cloud.logout()
        self.assertEqual(len(self.pending(cloud)), 1)
        self.sign_in(cloud, "second")
        cloud.logout()
        self.assertEqual(len(self.pending(cloud)), 2)
        self.sign_in(cloud, "current")
        cloud.shutdown()
        self.workers.clear()
        restarted = self.cloud()
        self.sign_in(restarted, "current")
        self.restore_revocations(restarted)
        self.run_worker()
        self.assertEqual(len(self.pending(restarted)), 1)
        self.run_worker()
        self.assertEqual(self.pending(restarted), {})
        self.assertEqual({call.kwargs["access_token"] for call in self.request.call_args_list}, {"first-access", "second-access"})
        self.assertEqual(restarted.token_store.load()["refreshToken"], "current-refresh")
        self.assertEqual(restarted.access_token, "current-access")
        self.assertTrue(restarted.authenticated)

    def test_offline_startup_logout_captures_stored_refresh_without_memory_session(self) -> None:
        cloud = self.cloud()
        cloud.token_store.save(token_response("stored"))
        cloud.logout()
        pending = next(iter(self.pending(cloud).values()))
        self.assertEqual(pending["refreshToken"], "stored-refresh")
        self.assertIsNone(cloud.token_store.load())
        self.request.side_effect = [token_response("rotated"), {}]
        self.run_worker()
        self.assertEqual(self.request.call_args_list[0].args[2], {"refreshToken": "stored-refresh"})
        self.assertEqual(self.pending(cloud), {})

    def test_rotated_revocation_credentials_survive_logout_failure_and_restart(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "expired")
        cloud.access_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        self.request.side_effect = [token_response("rotated"), ApiError("offline")]
        cloud.logout()
        self.run_worker()
        pending = next(iter(self.pending(cloud).values()))
        self.assertEqual(pending["refreshToken"], "rotated-refresh")
        self.assertEqual(pending["accessToken"], "rotated-access")
        cloud.shutdown()
        restarted = self.cloud()
        self.sign_in(restarted, "current")
        self.request.reset_mock()
        self.request.side_effect = [ApiError("expired", 401), token_response("retry"), {}]
        self.restore_revocations(restarted)
        self.run_worker()
        self.assertEqual(self.request.call_args_list[1].args[2], {"refreshToken": "rotated-refresh"})
        self.assertEqual(self.request.call_args_list[2].kwargs["access_token"], "retry-access")
        self.assertEqual(self.pending(restarted), {})
        self.assertEqual(restarted.token_store.load()["refreshToken"], "current-refresh")

    def test_enqueue_failure_preserves_active_credentials_and_existing_obligations(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "first")
        cloud.logout()
        self.sign_in(cloud, "current")
        before = self.pending(cloud)
        self.keyring.reject = lambda command, _input: command[1] == "store" and command[-1].startswith("oauth-revocations-v1:")
        with self.assertRaises(SecureStoreError):
            cloud.logout()
        self.assertTrue(cloud.authenticated)
        self.assertEqual(cloud.token_store.load()["refreshToken"], "current-refresh")
        self.assertEqual(self.pending(cloud), before)

    def test_rotation_save_failure_retries_new_credentials_before_logout(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "expired")
        cloud.access_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        cloud.logout()
        self.request.return_value = token_response("rotated")
        self.keyring.reject = lambda command, value: command[1] == "store" and value is not None and b"rotated-access" in base64.b64decode(value)
        self.run_worker()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(next(iter(self.pending(cloud).values()))["refreshToken"], "expired-refresh")
        self.keyring.reject = lambda _command, _input: False
        saved_at_logout = []
        self.request.side_effect = lambda *_args, **_kwargs: saved_at_logout.append(self.pending(cloud)) or {}
        self.retries.popleft()[1]()
        self.run_worker()
        saved = next(iter(saved_at_logout[0].values()))
        self.assertEqual(saved["refreshToken"], "rotated-refresh")
        self.assertEqual(self.request.call_args.kwargs["access_token"], "rotated-access")
        self.assertEqual(self.pending(cloud), {})

    def test_unauthorized_refresh_is_not_acknowledgment_and_never_uses_active_account(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        captured = cloud._accounts.pending_revocations()[0]
        self.sign_in(cloud, "current")
        self.request.side_effect = ApiError("unauthorized", 401)
        failures = []
        cloud.failure.connect(failures.append)
        for _attempt in range(5):
            self.run_worker()
            self.retries.popleft()[1]()
        pending = next(iter(self.pending(cloud).values()))
        self.assertFalse(pending["acknowledged"])
        self.assertEqual(pending["refreshToken"], "captured-refresh")
        self.assertEqual(self.request.call_args.args[2], {"refreshToken": "captured-refresh"})
        self.assertEqual(cloud.token_store.load()["refreshToken"], "current-refresh")
        self.assertNotIn("captured-refresh", repr(captured))
        self.assertNotIn("captured-access", repr(captured))
        self.assertEqual(failures, [])

    def test_clear_failure_leaves_durable_obligation_for_restart(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        self.keyring.reject = lambda command, _input: command[1] == "clear"
        with self.assertRaises(SecureStoreError):
            cloud.logout()
        self.assertEqual(len(self.pending(cloud)), 1)
        cloud.shutdown()
        self.keyring.reject = lambda _command, _input: False
        restarted = self.cloud()
        self.assertIsNone(restarted.token_store.load())
        self.restore_revocations(restarted)
        self.run_worker()
        self.assertEqual(self.pending(restarted), {})

    def test_acknowledged_cleanup_failure_resumes_without_another_network_request(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        self.keyring.reject = lambda command, value: command[1] == "store" and value is not None and json.loads(base64.b64decode(value)).get("pending") == {}
        self.run_worker()
        self.assertTrue(next(iter(self.pending(cloud).values()))["acknowledged"])
        self.assertEqual(self.request.call_count, 1)
        cloud.shutdown()
        restarted = self.cloud()
        self.keyring.reject = lambda _command, _input: False
        self.restore_revocations(restarted)
        self.run_worker()
        self.assertEqual(self.pending(restarted), {})
        self.assertEqual(self.request.call_count, 1)

    def test_acknowledgment_save_failure_retries_storage_without_repeating_logout(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        self.keyring.reject = lambda command, value: command[1] == "store" and value is not None and b'"acknowledged":true' in base64.b64decode(value)
        self.run_worker()
        self.assertFalse(next(iter(self.pending(cloud).values()))["acknowledged"])
        self.keyring.reject = lambda _command, _input: False
        self.retries.popleft()[1]()
        self.run_worker()
        self.assertEqual(self.pending(cloud), {})
        self.assertEqual(self.request.call_count, 1)

    def test_restore_load_failure_retries_without_touching_active_session(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        cloud.shutdown()
        self.workers.clear()
        restarted = self.cloud()
        self.sign_in(restarted, "current")
        self.keyring.reject = lambda command, _input: command[1] == "lookup" and command[-1].startswith("oauth-revocations-v1:")
        self.restore_revocations(restarted)
        self.assertEqual(restarted._revocation_restore_timer.interval(), 1000)
        for _attempt in range(10):
            restarted._revocation_restore_timer.timeout.emit()
            self.run_worker()
        self.assertEqual(restarted._revocation_restore_timer.interval(), 30000)
        self.keyring.reject = lambda _command, _input: False
        restarted._revocation_restore_timer.timeout.emit()
        self.run_worker()
        self.run_worker()
        self.assertEqual(self.pending(restarted), {})
        self.assertEqual(restarted.access_token, "current-access")

    def test_stale_restore_snapshot_cannot_recreate_acknowledged_job(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        stale = cloud._accounts.pending_revocations()
        self.run_worker()
        cloud._resume_revocations(stale)
        self.assertFalse(self.workers)
        self.assertEqual(self.pending(cloud), {})

    def test_inflight_failures_after_shutdown_do_not_schedule_new_work(self) -> None:
        cloud = self.cloud()
        self.sign_in(cloud, "captured")
        cloud.logout()
        cloud._restore_revocations()
        cloud.shutdown()
        self.keyring.reject = lambda command, _input: command[1] == "lookup"
        self.run_worker()
        self.run_worker()
        self.assertFalse(self.workers)
        self.assertFalse(self.retries)
        self.assertFalse(cloud._revocation_restore_timer.isActive())
        self.request.assert_not_called()
        self.keyring.reject = lambda _command, _input: False
        self.assertEqual(len(self.pending(cloud)), 1)


class RevocationWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.keyring = PlatformKeyring()
        self.enterContext(patch("pomodorough.secure_store.sys_platform", return_value="linux"))
        self.enterContext(patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"))
        self.enterContext(patch.object(PlatformSecretStore, "_run", side_effect=self.keyring.run))
        self.addCleanup(QThreadPool.globalInstance().waitForDone, 3000)

    def cloud(self, request: Mock) -> CloudService:
        tokens = TokenStore("worker-device", PlatformSecretStore(self.root), self.root / "session.json")
        cloud = CloudService("worker-device", "https://example.test", token_store=tokens, request=request)
        self.addCleanup(cloud.shutdown)
        return cloud

    def wait_until(self, predicate) -> None:
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate())

    def test_real_qt_startup_workers_and_backoff_resume_without_active_session(self) -> None:
        offline = Mock(side_effect=ApiError("offline"))
        cloud = self.cloud(offline)
        cloud._accept_tokens(token_response("captured"))
        cloud.logout()
        self.wait_until(lambda: offline.call_count == 1 and not cloud._revocation_workers)
        cloud.shutdown()
        retry = Mock(side_effect=[ApiError("offline"), {}])
        restarted = self.cloud(retry)
        self.wait_until(lambda: retry.call_count == 2 and not restarted._revocation_workers)
        self.assertEqual(restarted.token_store.revocations.load(restarted.api_base), {})
        self.assertIsNone(restarted.token_store.load())
        self.assertFalse(restarted.authenticated)
        self.assertEqual([call.kwargs["access_token"] for call in retry.call_args_list], ["captured-access"] * 2)

    def test_two_qt_services_never_recreate_acknowledged_job(self) -> None:
        first_request = Mock(return_value={})
        first = self.cloud(first_request)
        second_request = Mock(side_effect=ApiError("already revoked", 401))
        second = self.cloud(second_request)
        for cloud in (first, second):
            cloud._revocation_restore_timer.stop()
        captured = first._accounts.revocation("captured-access", "captured-refresh", True)
        stale = second._accounts.pending_revocations()
        first._resume_revocations([captured])
        self.wait_until(lambda: not first._revocation_workers)
        self.assertEqual(first.token_store.revocations.load(first.api_base), {})
        second._resume_revocations(stale)
        self.wait_until(lambda: not second._revocation_workers)
        self.assertEqual(second.token_store.revocations.load(second.api_base), {})
        first_request.assert_called_once()
        second_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
