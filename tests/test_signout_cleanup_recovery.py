from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_secure_store import linux_secret_store

from pomodorough.network import CloudService, TokenStore
from pomodorough.network_account import AccountLifecycle, SignOutCleanupError
from pomodorough.network_session import SessionState
from pomodorough.secure_store import PlatformSecretStore, SecureStoreError, TokenCleanupPendingError

API = "https://cleanup-recovery.example.test"


def response(account="old"):
    return {
        "accessToken": f"{account}-synthetic-access",
        "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
        "refreshToken": f"{account}-synthetic-refresh",
        "refreshTokenExpiresAt": "2099-02-01T00:00:00Z",
    }


class FileVault:
    def __init__(self, root):
        self.directory = root
        (root / "fixture-vault").mkdir(exist_ok=True)

    def path(self, key):
        return self.directory / "fixture-vault" / hashlib.sha256(key.encode()).hexdigest()

    def load(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def save(self, key, value):
        self.path(key).write_bytes(value)

    def delete(self, key):
        with (self.directory / "delete-attempts").open("a") as attempts:
            attempts.write(key + "\n")
        if (self.directory / "reject-delete").exists():
            raise SecureStoreError("old-synthetic-access old-synthetic-refresh")
        self.path(key).unlink(missing_ok=True)


class LockedFileVault(FileVault, PlatformSecretStore):
    def __init__(self, root):
        PlatformSecretStore.__init__(self, root / str(os.getpid()))
        FileVault.__init__(self, root)


def forbidden(*_args, **_kwargs):
    raise AssertionError("Native vault or external network access is forbidden")


@contextmanager
def fixture_environment(root):
    with (
        linux_secret_store(root),
        patch("pomodorough.network.shutil.which", return_value=None),
        patch("pomodorough.secure_store._macos_load", side_effect=forbidden),
        patch("pomodorough.secure_store._macos_save", side_effect=forbidden),
        patch("pomodorough.secure_store._macos_delete", side_effect=forbidden),
        patch("pomodorough.secure_store.PlatformSecretStore._run", side_effect=forbidden),
        patch("urllib.request.urlopen", side_effect=forbidden),
    ):
        yield


def token_store(root, native=False):
    vault = LockedFileVault(root) if native else FileVault(root)
    tokens = TokenStore("cleanup-device", vault, root / "session.json")
    tokens.bind_api(API)
    return tokens


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    services, retries = [], []
    monkeypatch.setattr(QThreadPool.globalInstance(), "start", lambda _worker: None)
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: retries.append((delay, callback)))

    def create():
        cloud = CloudService("cleanup-device", API, token_store=token_store(tmp_path), request=Mock())
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        services.append(cloud)
        return cloud

    with fixture_environment(tmp_path):
        yield SimpleNamespace(root=tmp_path, app=app, create=create, retries=retries)
        for cloud in services:
            cloud.shutdown()


def pending_logout(recovery):
    cloud = recovery.create()
    cloud._accept_login_tokens(response())
    cloud.authenticated = True
    (recovery.root / "reject-delete").touch()
    with pytest.raises(SignOutCleanupError):
        cloud.logout()
    (recovery.root / "reject-delete").unlink()
    return cloud


@pytest.mark.parametrize("writer", ["same-store", "separate-store", "separate-cloud"])
def test_cleanup_check_and_delete_serialize_with_replacement_save(recovery, monkeypatch, writer):
    cloud = pending_logout(recovery)
    replacement = recovery.create()
    tokens = cloud.token_store if writer == "same-store" else replacement.token_store
    checked, release, saving = threading.Event(), threading.Event(), threading.Event()
    original = cloud.token_store._load_cleanup_tombstone

    def checked_tombstone():
        observed = original()
        checked.set()
        assert release.wait(5)
        return observed

    def save():
        saving.set()
        if writer == "separate-cloud":
            replacement._accept_login_tokens(response("new"))
            replacement.authenticated = True
        else:
            tokens.save(response("new"))

    monkeypatch.setattr(cloud.token_store, "_load_cleanup_tombstone", checked_tombstone)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup = executor.submit(cloud._accounts.retry_sign_out_cleanup, cloud._account_generation)
        try:
            assert checked.wait(5)
            saved = executor.submit(save)
            assert saving.wait(5)
            assert not saved.done()
        finally:
            release.set()
        cleanup.result(timeout=5)
        saved.result(timeout=5)
    assert tokens.load()["refreshToken"] == "new-synthetic-refresh"
    assert len(tokens.revocations.load(API)) == 1
    if writer == "separate-cloud":
        assert replacement.authenticated and replacement.access_token == "new-synthetic-access"


def test_save_removes_tombstone_before_waiting_cleanup_can_inspect_it(recovery, monkeypatch):
    cloud = pending_logout(recovery)
    replacement = recovery.create()
    written, release, cleaning = threading.Event(), threading.Event(), threading.Event()
    vault = replacement.token_store.secret_store
    original = vault.save

    def paused_save(key, value):
        original(key, value)
        written.set()
        assert release.wait(5)

    def cleanup():
        cleaning.set()
        cloud._accounts.retry_sign_out_cleanup(cloud._account_generation)

    monkeypatch.setattr(vault, "save", paused_save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = executor.submit(replacement.token_store.save, response("new"))
        try:
            assert written.wait(5)
            cleaned = executor.submit(cleanup)
            assert cleaning.wait(5)
            assert not cleaned.done()
        finally:
            release.set()
        saved.result(timeout=5)
        cleaned.result(timeout=5)
    assert replacement.token_store.load()["refreshToken"] == "new-synthetic-refresh"


def process_mutation(root, operation, native, ready, release, completed):
    with fixture_environment(root):
        tokens = token_store(root, native)
        if operation == "save":
            ready.set()
            tokens.save(response("new"))
        else:
            original = tokens._load_cleanup_tombstone

            def checked_tombstone():
                observed = original()
                ready.set()
                assert release.wait(10)
                return observed

            account = AccountLifecycle(API, SessionState(), tokens, forbidden, lambda text: text,
                                       lambda: datetime.now(UTC), tokens.revocations)
            with patch.object(tokens, "_load_cleanup_tombstone", checked_tombstone):
                account.retry_sign_out_cleanup(0)
        completed.set()


@pytest.mark.parametrize("native", [False, True], ids=["fallback-file-lock", "platform-and-file-lock"])
def test_fresh_process_save_waits_for_atomic_cleanup(tmp_path, native):
    with fixture_environment(tmp_path):
        tokens = token_store(tmp_path, native)
        tokens.save(response())
        (tmp_path / "reject-delete").touch()
        with pytest.raises(TokenCleanupPendingError):
            tokens.clear()
        (tmp_path / "reject-delete").unlink()
        context = multiprocessing.get_context("spawn")
        ready, release, saving, cleaned, saved = [context.Event() for _index in range(5)]
        cleanup = context.Process(target=process_mutation, args=(tmp_path, "cleanup", native, ready, release, cleaned))
        writer = context.Process(target=process_mutation, args=(tmp_path, "save", native, saving, release, saved))
        cleanup.start()
        try:
            assert ready.wait(10)
            writer.start()
            assert saving.wait(10)
            assert not saved.wait(0.2)
        finally:
            release.set()
            for process in (cleanup, writer):
                if process.pid is not None:
                    process.join(10)
                    if process.is_alive():
                        process.kill()
                        process.join(5)
        assert cleanup.exitcode == writer.exitcode == 0
        assert cleaned.is_set() and saved.is_set()
        assert tokens.load()["refreshToken"] == "new-synthetic-refresh"


@pytest.mark.parametrize("transition", ["login", "shutdown", "generation", "stored-other-origin"])
def test_startup_cleanup_obeys_session_fences(recovery, transition):
    cloud = pending_logout(recovery)
    restarted = recovery.create()
    if transition == "login":
        restarted._accept_login_tokens(response("new"))
        restarted.authenticated = True
    elif transition == "shutdown":
        restarted.shutdown()
    elif transition == "generation":
        restarted._account_generation += 1
    else:
        tokens = TokenStore("cleanup-device", FileVault(recovery.root), recovery.root / "session.json")
        tokens.bind_api("https://replacement.example.test")
        tokens.save(response("new"))
    before = cloud.token_store.secret_store.load(cloud.token_store.secret_key)
    with patch.object(restarted.token_store.secret_store, "delete", side_effect=AssertionError("stale cleanup")):
        restarted._sign_out_cleanup_timer.timeout.emit()
    assert cloud.token_store.secret_store.load(cloud.token_store.secret_key) == before


@pytest.mark.parametrize("contents", ["invalid", "[]", "null", '{"signedOut":'])
def test_corrupt_tombstone_neither_restores_nor_deletes_old_secret(recovery, contents):
    cloud = pending_logout(recovery)
    cloud.token_store.fallback_path.write_text(contents)
    before = cloud.token_store.secret_store.load(cloud.token_store.secret_key)
    with pytest.raises(SecureStoreError, match="malformed"):
        cloud.token_store.load()
    with pytest.raises(SecureStoreError, match="retried"):
        cloud._accounts.retry_sign_out_cleanup(cloud._account_generation)
    assert cloud.token_store.secret_store.load(cloud.token_store.secret_key) == before


def test_unreadable_tombstone_reports_retry_without_restoring_or_deleting(recovery):
    cloud = pending_logout(recovery)
    before = cloud.token_store.secret_store.load(cloud.token_store.secret_key)
    failures = []
    cloud.failure.connect(failures.append)
    with patch.object(Path, "read_text", side_effect=PermissionError("old-synthetic-refresh")):
        with pytest.raises(PermissionError):
            cloud.token_store.load()
        cloud._retry_sign_out_cleanup(cloud._account_generation)
    assert len(failures) == 1 and "will be retried" in failures[0]
    assert "old-synthetic" not in failures[0]
    assert cloud.token_store.secret_store.load(cloud.token_store.secret_key) == before


def startup_process(root, phase):
    app = QApplication.instance() or QApplication([])
    with fixture_environment(root):
        tokens = token_store(root)
        requests, failures = [], []

        def request(method, url, *_args, **_kwargs):
            assert method == "POST" and url == API + "/api/v1/auth/logout"
            requests.append(url)
            return {}

        cloud = CloudService("cleanup-device", API, token_store=tokens, request=request)
        cloud.failure.connect(failures.append)
        if phase == "crash":
            cloud._accept_login_tokens(response())
            cloud.authenticated = True
            (root / "reject-delete").touch()
            try:
                cloud.logout()
            except SignOutCleanupError:
                os._exit(0)
            raise AssertionError("Secure deletion should fail")
        if phase == "recovered":
            (root / "reject-delete").unlink(missing_ok=True)
        cloud.restore()
        deadline = time.monotonic() + (2.6 if phase == "unavailable" else 0.6)
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        result = {
            "authenticated": cloud.authenticated, "loaded": tokens.load(),
            "secret": tokens.secret_store.load(tokens.secret_key) is not None,
            "deletes": len((root / "delete-attempts").read_text().splitlines()) - 1,
            "pending": len(tokens.revocations.load(API)),
            "requests": requests, "failures": failures,
        }
        cloud.shutdown()
        assert QThreadPool.globalInstance().waitForDone(5000)
        print(json.dumps(result), flush=True)


def run_startup(root, phase):
    command = [sys.executable, str(Path(__file__).resolve()), phase, str(root)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}) as process:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
    return json.loads(stdout) if stdout else None


@pytest.mark.parametrize("phase", ["recovered", "unavailable"])
def test_crash_restart_resumes_and_reports_cleanup_without_logout(tmp_path, phase):
    run_startup(tmp_path, "crash")
    restarted = run_startup(tmp_path, phase)
    assert not restarted["authenticated"] and restarted["loaded"] is None
    assert restarted["pending"] == 0
    assert restarted["requests"] == [API + "/api/v1/auth/logout"]
    assert restarted["deletes"] >= 1
    if phase == "recovered":
        assert not restarted["secret"] and not restarted["failures"]
    else:
        assert restarted["secret"] and restarted["deletes"] >= 2
        assert all("will be retried" in error for error in restarted["failures"])
        assert len(restarted["failures"]) >= 2
        assert "old-synthetic" not in repr(restarted["failures"])
        recovered = run_startup(tmp_path, "recovered")
        assert not recovered["secret"] and not recovered["failures"]
        assert recovered["pending"] == 0 and recovered["requests"] == []


if __name__ == "__main__":
    startup_process(Path(sys.argv[2]), sys.argv[1])
