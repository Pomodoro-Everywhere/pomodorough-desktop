"""R53-D03: busy retry eventually restores via the real Qt event loop."""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication
from test_r52_d03_restore_retry import _MemorySecretStore
from test_signout_cleanup_recovery import response

from pomodorough.network import ApiError, CloudService, TokenStore


def _pump(app, ready, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if ready():
            return True
        time.sleep(0.005)
    app.processEvents()
    return ready()


def _start_offline_restore(app, cloud, state):
    cloud.restore()
    assert _pump(app, lambda: cloud._restore_attempt == 1, 5.0)
    assert len(state["requests"]) == 1


def _hold_busy_through_first_retry(app, cloud, state):
    deadline = time.monotonic() + 1.6
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert cloud._restore_attempt == 1
    assert len(state["requests"]) == 1


def test_busy_retry_eventually_restores_after_worker_completion(tmp_path):
    app = QApplication.instance() or QApplication([])
    state = {"online": False, "requests": [], "users": []}

    def request(method, url, *args, **kwargs):
        state["requests"].append(url)
        if not state["online"]:
            raise ApiError("synthetic offline startup")
        return response() if url.endswith("/refresh") else {"user": {"id": "stored"}}

    tokens = TokenStore("r53-busy-loop", _MemorySecretStore(), tmp_path / "session.json")
    cloud = CloudService("r53-busy-loop", "https://restore.test", token_store=tokens,
                         request=request)
    release = threading.Event()
    try:
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud.signed_in.connect(state["users"].append)
        cloud._accept_login_tokens(response())
        with patch.object(cloud, "start_revision_stream"):
            _start_offline_restore(app, cloud, state)
            state["online"] = True

            def authorize(**kwargs):
                assert release.wait(6.0)
                raise ApiError("synthetic browser cancelled")

            with patch.object(cloud, "_authorize_google", side_effect=authorize):
                cloud.login()
                assert _pump(app, lambda: cloud.busy, 5.0)
                _hold_busy_through_first_retry(app, cloud, state)
                release.set()
                assert _pump(app, lambda: not cloud.busy, 5.0)
            assert _pump(app, lambda: cloud.authenticated, 6.0)
            assert state["users"] == [{"id": "stored"}]
            assert state["requests"] == [
                "https://restore.test/api/v1/me",
                "https://restore.test/api/v1/auth/refresh",
                "https://restore.test/api/v1/me",
            ]
            assert cloud._restore_attempt == 0
    finally:
        release.set()
        cloud.shutdown()
        assert QThreadPool.globalInstance().waitForDone(5000)
