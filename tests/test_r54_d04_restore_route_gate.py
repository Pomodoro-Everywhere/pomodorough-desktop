"""R54-D04: replication route restores defer to an in-flight login."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication
from test_r52_d01_reset_fence import OWNER
from test_r52_d03_restore_retry import _MemorySecretStore
from test_replication_controller import ControllerHarness, FakeIroh
from test_signout_cleanup_recovery import response

from pomodorough.network import ApiError, CloudService, TokenStore


@pytest.fixture
def gate(tmp_path):
    """Real CloudService._start: workers are captured and busy stays set until they run."""
    app = QApplication.instance() or QApplication([])
    state = SimpleNamespace(
        timers=[], requests=[], users=[], expired=[], refresh="rotate",
        land_login=lambda: None,
    )

    def request(method, url, *args, **kwargs):
        state.requests.append(url)
        if not url.endswith("/refresh"):
            return {"user": {"id": "stored"}}
        state.land_login()  # the login completes while this refresh is on the wire
        if state.refresh == "reject":
            raise ApiError("synthetic refresh rejected", status=401)
        return response("rotated")

    def authorize(expected_generation=None):
        cloud._accept_login_tokens(response("login"), expected_generation)
        return OWNER

    tokens = TokenStore("r54-gate", _MemorySecretStore(), tmp_path / "session.json")
    cloud = CloudService("r54-gate", "https://gate.test", token_store=tokens, request=request)
    cloud._revocation_restore_timer.stop()
    cloud._sign_out_cleanup_timer.stop()
    cloud._accept_login_tokens(response())
    # The saved session's access token is stale, so any restore must refresh it.
    cloud._state.access_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    cloud.signed_in.connect(state.users.append)
    cloud.session_expired.connect(lambda: state.expired.append(True))
    harness = ControllerHarness(iroh=FakeIroh())
    harness.cloud = cloud
    harness.store.active_iroh_room_id = "saved-room"
    harness.controller.mode = harness.store.replication_mode = "centralized"
    workers = []
    with (
        patch("pomodorough.network.QTimer.singleShot",
              side_effect=lambda ms, cb: state.timers.append((ms, cb))),
        patch.object(QThreadPool.globalInstance(), "start", workers.append),
        patch.object(cloud, "_authorize_google", side_effect=authorize),
        patch.object(cloud._revisions, "start") as stream,
    ):
        try:
            yield harness, cloud, state, workers, stream
        finally:
            cloud.shutdown()
    assert app is not None


def start_failed_join(harness):
    harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    harness.iroh.join_pending = False  # the runtime gave up on the join


def stop_iroh_for_cloud(harness):
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.restore_replication()
    harness.controller.replication_mode_changed("centralized")
    assert harness.controller.cloud_restore_after_iroh_stop


def start_offline(harness):
    harness.controller.mode = harness.store.replication_mode = "offline"
    harness.controller.restore_replication()


# Each site is a (prepare, fire) pair; fire is the call that re-enters the cloud route.
SITES = {
    "join_status": (start_failed_join, lambda c: c.iroh_status_changed("NOT CONNECTED")),
    "iroh_stop_status": (stop_iroh_for_cloud, lambda c: c.iroh_status_changed("NOT CONNECTED")),
    "join_failure": (start_failed_join, lambda c: c.iroh_failure("join failed")),
    "offline_switch": (start_offline, lambda c: c.replication_mode_changed("centralized")),
}


def run_pending(workers, start):
    while start < len(workers):
        workers[start].run()
        start += 1
    return start


def settle(state, workers):
    """Land the login, inside any restore refresh, then fire the deferred retries."""
    def land_login():
        state.land_login = lambda: None
        workers[0].run()

    state.land_login = land_login
    ran = run_pending(workers, 1)
    state.land_login()
    for _delay, callback in list(state.timers):
        callback()
    run_pending(workers, ran)


def session_outcome(cloud, state, workers, stream):
    # The store persists only the refresh token; the access token is memory-only.
    stored = cloud.token_store.load() or {}
    return {
        "restore_workers": len(workers) - 1,
        "access": cloud._state.access_token,
        "refresh": stored.get("refreshToken"),
        "expired": bool(state.expired),
        "authenticated": cloud.authenticated,
        "users": state.users,
        "streams": stream.call_count,
    }


LOGIN_KEPT = {
    "restore_workers": 0,
    "access": "login-synthetic-access",
    "refresh": "login-synthetic-refresh",
    "expired": False,
    "authenticated": True,
    "users": [OWNER],
    "streams": 1,
}


@pytest.mark.parametrize("refresh", ["rotate", "reject"])
@pytest.mark.parametrize("site", SITES)
def test_route_site_during_login_keeps_login_session(gate, site, refresh):
    harness, cloud, state, workers, stream = gate
    prepare, fire = SITES[site]
    state.refresh = refresh
    prepare(harness)
    cloud.login()
    assert cloud.busy and len(workers) == 1
    fire(harness.controller)
    settle(state, workers)
    assert harness.controller.mode == "centralized"
    assert session_outcome(cloud, state, workers, stream) == LOGIN_KEPT


@pytest.mark.parametrize("site", SITES)
def test_route_site_without_login_restores_now(gate, site):
    harness, cloud, state, workers, stream = gate
    prepare, fire = SITES[site]
    prepare(harness)
    assert workers == [] and not cloud.busy
    fire(harness.controller)
    assert len(workers) == 1 and state.timers == []
    workers[0].run()
    assert harness.controller.mode == "centralized"
    assert cloud.authenticated and state.users == [{"id": "stored"}]
    assert cloud._state.access_token == "rotated-synthetic-access"
    assert cloud.token_store.load()["refreshToken"] == "rotated-synthetic-refresh"
    assert state.requests[-1].endswith("/api/v1/me") and state.expired == []
    stream.assert_called_once()


def test_second_busy_reentry_retires_first_retry_chain(gate):
    # Each busy resume_restore bumps the restore epoch, so a second re-entry in
    # one busy window retires the first deferred retry instead of doubling it.
    _harness, cloud, state, workers, stream = gate
    cloud.login()
    assert cloud.busy and len(workers) == 1
    cloud.resume_restore()
    cloud.resume_restore()
    armed, state.timers = state.timers, []
    for _delay, callback in armed:
        callback()  # still busy: a live chain re-arms, a retired chain returns
    assert (len(armed), len(state.timers)) == (2, 1)
    settle(state, workers)
    assert session_outcome(cloud, state, workers, stream) == LOGIN_KEPT
