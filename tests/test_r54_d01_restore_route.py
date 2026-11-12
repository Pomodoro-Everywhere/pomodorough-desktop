"""R54-D01: retired replication routes cannot restore cloud sessions."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_r52_d01_reset_fence import (
    OWNER,
    _blocked_workers,
    _close,
    _quiet,
    _seed,
    _window,
)
from test_r52_d03_restore_retry import _MemorySecretStore, _run_immediately
from test_replication_controller import ControllerHarness, FakeIroh
from test_signout_cleanup_recovery import FileVault, fixture_environment, response

from pomodorough.network import ApiError, CloudService, TokenStore
from pomodorough.storage import Store

_SINGLE_SHOT = QTimer.singleShot


@pytest.fixture
def route(tmp_path):
    app = QApplication.instance() or QApplication([])
    state = SimpleNamespace(timers=[], requests=[], users=[], online=False)

    def request(method, url, *args, **kwargs):
        state.requests.append(url)
        if not state.online:
            raise ApiError("synthetic offline startup")
        return response() if url.endswith("/refresh") else {"user": {"id": "stored"}}

    tokens = TokenStore("r54-route", _MemorySecretStore(), tmp_path / "session.json")
    cloud = CloudService("r54-route", "https://restore.test", token_store=tokens,
                         request=request)
    cloud._revocation_restore_timer.stop()
    cloud._sign_out_cleanup_timer.stop()
    cloud._accept_login_tokens(response())
    cloud.signed_in.connect(state.users.append)
    harness = ControllerHarness(iroh=FakeIroh())
    harness.cloud = cloud
    harness.store.active_iroh_room_id = "saved-room"
    harness.controller.mode = harness.store.replication_mode = "centralized"
    # _run_immediately never sets busy, so the UI's busy refusal of departures
    # does not apply here. Cases that depart while a restore worker runs are
    # defense-in-depth pins for the epoch fences, not UI reproductions.
    with (
        patch("pomodorough.network.QTimer.singleShot",
              side_effect=lambda ms, cb: state.timers.append((ms, cb))),
        patch.object(cloud, "_start", side_effect=_run_immediately),
        patch.object(cloud._revisions, "start") as stream,
    ):
        try:
            yield harness, cloud, state, stream
        finally:
            cloud.shutdown()
    assert app is not None


def return_to_cloud(harness, cloud, state, stream):
    state.online = True
    harness.controller.replication_mode_changed("centralized")
    if harness.controller.cloud_restore_after_iroh_stop:
        harness.controller.iroh_status_changed("NOT CONNECTED")
    assert cloud.authenticated
    assert state.users == [{"id": "stored"}]
    assert cloud.token_store.load() is not None
    stream.assert_called_once()


@pytest.mark.parametrize("mode", ["offline", "iroh"])
@pytest.mark.parametrize("busy_deferral", [False, True])
def test_departure_cancels_retry_and_reentry_restores(route, mode, busy_deferral):
    harness, cloud, state, stream = route
    cloud.restore()
    credentials = cloud.token_store.load()
    if busy_deferral:
        cloud.busy = True
        state.timers.pop(0)[1]()
        cloud.busy = False
    harness.controller.replication_mode_changed(mode)
    assert harness.store.replication_mode == mode
    state.online = True
    state.timers.pop(0)[1]()
    assert len(state.requests) == 1
    assert not cloud.authenticated and state.users == []
    assert cloud.token_store.load() == credentials
    cloud.start_revision_stream()
    stream.assert_not_called()
    return_to_cloud(harness, cloud, state, stream)


@pytest.mark.parametrize("result", ["success", "offline", "unauthorized"])
@pytest.mark.parametrize("reenter_first", [False, True])
def test_queued_completion_is_fenced_across_route_epoch(route, result, reenter_first):
    # Defense-in-depth: the UI refuses this departure while the worker runs.
    harness, cloud, state, stream = route
    callbacks = []
    with patch.object(cloud, "_start", side_effect=lambda *args: callbacks.append(args)):
        cloud.restore()
    work, success, failure = callbacks.pop()
    state.online = True
    user = work()
    credentials = cloud.token_store.load()
    harness.controller.replication_mode_changed("offline")
    if reenter_first:
        return_to_cloud(harness, cloud, state, stream)
    before = (cloud.authenticated, list(state.users), stream.call_count)
    if result == "success":
        success(user)
    else:
        failure(ApiError("synthetic stale failure", status=401 if result == "unauthorized" else None))
    assert (cloud.authenticated, state.users, stream.call_count) == before
    assert cloud.token_store.load() == credentials
    assert state.timers == []
    if not reenter_first:
        return_to_cloud(harness, cloud, state, stream)


def test_retired_worker_does_not_send_requests(route):
    # Defense-in-depth: the UI refuses this departure while the worker runs.
    harness, cloud, state, stream = route
    callbacks = []
    with patch.object(cloud, "_start", side_effect=lambda *args: callbacks.append(args)):
        cloud.restore()
    harness.controller.replication_mode_changed("offline")
    work, success, _ = callbacks.pop()
    success(work())
    assert state.requests == state.users == []
    assert cloud.token_store.load() is not None
    stream.assert_not_called()


def test_departure_during_refresh_does_not_request_user(route):
    # Defense-in-depth: the UI refuses this departure while the worker runs.
    harness, cloud, state, stream = route
    with patch.object(cloud, "_ensure_access", side_effect=lambda: (
        harness.controller.replication_mode_changed("offline"), "access"
    )[1]):
        cloud.restore()
    assert state.requests == state.users == []
    assert not cloud.authenticated
    stream.assert_not_called()


def test_departure_retires_pending_iroh_stop_restore(route):
    harness, cloud, state, stream = route
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.replication_mode_changed("centralized")
    assert harness.controller.cloud_restore_after_iroh_stop
    harness.controller.replication_mode_changed("offline")
    harness.controller.iroh_status_changed("NOT CONNECTED")
    assert state.requests == state.users == []
    assert not cloud.authenticated
    stream.assert_not_called()


def test_create_room_cancels_scheduled_restore(route):
    harness, cloud, state, stream = route
    cloud.restore()
    harness.controller.create_iroh_room("route test")
    assert harness.controller.mode == "iroh"
    state.timers.pop(0)[1]()
    assert len(state.requests) == 1
    assert state.users == []
    stream.assert_not_called()


def test_busy_worker_completion_cannot_revive_retired_retry(route):
    harness, cloud, state, stream = route
    cloud.restore()
    workers = []
    with (
        patch.object(cloud, "_start", CloudService._start.__get__(cloud)),
        patch.object(QThreadPool.globalInstance(), "start", workers.append),
    ):
        cloud._start(lambda: None, lambda result: None)
        state.timers.pop(0)[1]()
        assert cloud.busy and len(state.timers) == 1
        workers.pop().run()
        assert not cloud.busy and cloud._workers == set()
        harness.controller.replication_mode_changed("offline")
        state.timers.pop(0)[1]()
        assert workers == []
    assert len(state.requests) == 1 and state.users == []
    return_to_cloud(harness, cloud, state, stream)


class _Network:
    def __init__(self):
        self.online = False
        self.requests = []

    def __call__(self, method, url, *args, **kwargs):
        path = url.split(".test", 1)[1]
        self.requests.append(path)
        if not self.online:
            raise ApiError("synthetic offline")
        if path.endswith("/auth/refresh"):
            return response("rotated")
        return {"user": OWNER} if path.endswith("/me") else {}


@contextmanager
def real_route(tmp_path):
    """Real Store, TokenStore, CloudService, and MainWindow in the cloud route."""
    app = QApplication.instance() or QApplication([])
    net, timers = _Network(), []

    def single_shot(ms, callback, *rest):
        if "CloudService" not in getattr(callback, "__qualname__", ""):
            return _SINGLE_SHOT(ms, callback, *rest)
        timers.append(callback)
        return None

    with fixture_environment(tmp_path), _quiet(), _blocked_workers():
        vault = FileVault(tmp_path)
        store = Store(tmp_path / "state.sqlite3", iroh_secret_store=vault)
        _seed(store)
        store.set_replication_mode("centralized")
        tokens = TokenStore(store.device_id, vault, tmp_path / "session.json")
        cloud = CloudService(store.device_id, "https://r54-route.test",
                             token_store=tokens, request=net)
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud._accept_login_tokens(response())
        window, _, _ = _window(app, store, cloud)
        window.iroh = FakeIroh()
        # _blocked_workers parked the startup restore; retire it.
        cloud._workers.clear()
        cloud._worker_generations.clear()
        cloud.busy = cloud.authenticated = False
        net.requests.clear()
        users = []
        cloud.signed_in.connect(users.append)
        try:
            with (
                patch.object(QTimer, "singleShot", side_effect=single_shot),
                patch.object(cloud, "_start", side_effect=_run_immediately),
                patch.object(cloud._revisions, "start", side_effect=lambda *_: setattr(
                    cloud._revisions.state, "reply", MagicMock(name="revision-reply"),
                )) as stream,
            ):
                yield SimpleNamespace(app=app, store=store, tokens=tokens, cloud=cloud,
                                      window=window, net=net, timers=timers,
                                      users=users, stream=stream)
        finally:
            _close(cloud, window, store)


def test_real_workspace_offline_departure_preserves_credentials(tmp_path):
    with real_route(tmp_path) as env:
        credentials = env.tokens.load()
        env.cloud.restore()
        assert env.net.requests == ["/api/v1/me"] and len(env.timers) == 1
        env.window._replication_mode_changed("offline")
        assert env.store.replication_mode == "offline"
        env.net.online = True
        env.timers.pop(0)()
        assert env.net.requests == ["/api/v1/me"] and env.timers == []
        assert env.users == [] and not env.cloud.authenticated
        assert env.tokens.load() == credentials
        env.stream.assert_not_called()
        env.window._replication_mode_changed("centralized")
        assert env.net.requests[1:3] == ["/api/v1/auth/refresh", "/api/v1/me"]
        assert env.cloud.authenticated and env.users == [OWNER]
        env.stream.assert_called_once()


def test_real_window_join_cancel_during_login_restores_stream(tmp_path):
    with real_route(tmp_path) as env:
        env.net.online = True
        env.window.replication._start_iroh_join(SimpleNamespace(room_id="joining-room"))
        workers = []

        def authorize(expected_generation=None):
            env.cloud._accept_login_tokens(response("login"), expected_generation)
            return OWNER

        with (
            patch.object(env.cloud, "_start", CloudService._start.__get__(env.cloud)),
            patch.object(QThreadPool.globalInstance(), "start", workers.append),
            patch.object(env.cloud, "_authorize_google", side_effect=authorize),
        ):
            env.window._account_action()
            assert env.cloud.busy and len(workers) == 1
            env.window._replication_mode_changed("centralized")
            assert env.window.replication.mode == env.store.replication_mode == "centralized"
            assert not env.window.replication.iroh_join_pending
            workers.pop().run()
        for _ in range(3):
            if env.timers:
                env.timers.pop(0)()
        assert env.timers == [] and env.net.requests == []
        assert env.cloud.authenticated and env.users == [OWNER]
        env.stream.assert_called_once()


def start_join_from_live_cloud(harness, cloud, state, stream):
    state.online = True
    harness.controller.restore_replication()
    assert cloud.authenticated
    stream.assert_called_once()
    stream.reset_mock()
    harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    assert harness.controller.iroh_join_pending


def assert_cancelled_into_cloud(harness):
    assert harness.controller.mode == harness.store.replication_mode == "centralized"
    assert not harness.controller.iroh_join_pending


@pytest.mark.parametrize("target", ["centralized", "offline"])
def test_join_cancel_refused_while_busy_resumes_route(route, target):
    harness, cloud, state, stream = route
    start_join_from_live_cloud(harness, cloud, state, stream)
    cloud.busy = True
    harness.controller.replication_mode_changed(target)
    cloud.busy = False
    assert_cancelled_into_cloud(harness)
    assert state.timers == []
    cloud.start_revision_stream()  # a later sync or login completion
    stream.assert_called_once()


def test_join_cancel_while_busy_signed_out_restores_after_worker(route):
    harness, cloud, state, stream = route
    harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    cloud.busy = True
    harness.controller.replication_mode_changed("centralized")
    assert_cancelled_into_cloud(harness)
    state.online = True
    for busy in (True, False):
        cloud.busy = busy
        deferred, state.timers[:] = list(state.timers), []
        for _, callback in deferred:
            callback()
        if busy:
            assert state.requests == []
    assert cloud.authenticated and state.users == [{"id": "stored"}]
    stream.assert_called_once()


@pytest.mark.parametrize("action", ["mode_change", "leave"])
@pytest.mark.parametrize("failure", [OSError("persist failed"), ValueError("bad mode")])
def test_join_cancel_persist_failure_resumes_route(route, action, failure):
    harness, cloud, state, stream = route
    start_join_from_live_cloud(harness, cloud, state, stream)
    harness.store.failure = failure
    if action == "leave":
        outcome = harness.controller.leave_iroh_room()
    else:
        outcome = harness.controller.replication_mode_changed("offline")
    harness.store.failure = None
    assert outcome.effects[0].error is failure
    assert_cancelled_into_cloud(harness)
    assert cloud.authenticated
    stream.assert_called_once()


def test_join_retires_pending_iroh_stop_restore(route):
    harness, cloud, state, stream = route
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.replication_mode_changed("centralized")
    assert harness.controller.cloud_restore_after_iroh_stop
    harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    state.online = True
    harness.controller.iroh_status_changed("NOT CONNECTED")
    assert harness.controller.iroh_join_pending
    assert state.requests == state.users == [] and not cloud.authenticated
    stream.assert_not_called()


def test_unpackaged_iroh_route_reentry_restores_cloud(route):
    harness, cloud, state, stream = route
    harness.iroh = None
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.restore_replication()
    assert not harness.controller.cloud_restore_after_iroh_stop
    return_to_cloud(harness, cloud, state, stream)


def test_refresh_rejection_after_departure_reports_expiry(route):
    # Defense-in-depth: the UI refuses this departure while the worker runs.
    harness, cloud, state, stream = route
    expired = []
    cloud.session_expired.connect(lambda: expired.append(True))
    cloud._state.access_expires_at = datetime(2000, 1, 1, tzinfo=UTC)

    def reject(*args, **kwargs):
        harness.controller.replication_mode_changed("offline")
        raise ApiError("synthetic refresh rejected", status=401)

    with patch.object(cloud._session, "request", side_effect=reject):
        cloud.restore()
    assert harness.store.replication_mode == "offline"
    assert cloud.token_store.load() is None
    assert expired == [True]
    assert not cloud.authenticated and state.users == [] and state.timers == []
    assert state.requests == []
    stream.assert_not_called()


def open_live_stream(harness, cloud, state, stream):
    replies = []

    def open_reply(*_args):
        replies.append(MagicMock(name="revision-reply"))
        cloud._revisions.state.reply = replies[-1]

    stream.side_effect = open_reply
    state.online = True
    harness.controller.restore_replication()
    assert cloud.authenticated and len(replies) == 1
    return replies


def login_owner(cloud):
    def authorize(expected_generation=None):
        cloud._accept_login_tokens(response("login"), expected_generation)
        return OWNER

    with patch.object(cloud, "_authorize_google", side_effect=authorize):
        cloud.login()


@pytest.mark.parametrize("departure", ["offline", "join"])
def test_departure_stops_live_stream_and_retires_route(route, departure):
    harness, cloud, state, stream = route
    replies = open_live_stream(harness, cloud, state, stream)
    if departure == "join":
        harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    else:
        harness.controller.replication_mode_changed("offline")
    replies[0].abort.assert_called_once()
    cloud.start_revision_stream()  # a later sync or login completion
    assert len(replies) == 1
    harness.controller.replication_mode_changed("centralized")
    assert harness.controller.mode == "centralized"
    assert len(replies) == 2


@pytest.mark.parametrize("mode", ["offline", "iroh"])
def test_noncentralized_startup_login_keeps_stream_off(route, mode):
    harness, cloud, state, stream = route
    harness.controller.mode = harness.store.replication_mode = mode
    harness.controller.restore_replication()
    state.online = True
    login_owner(cloud)
    assert cloud.authenticated and state.users == [OWNER]
    assert state.requests == [] and state.timers == []
    stream.assert_not_called()


@pytest.mark.parametrize("iroh", ["unpackaged", "unavailable", "no_room"])
def test_iroh_startup_without_runtime_keeps_stream_off_after_login(route, iroh):
    harness, cloud, state, stream = route
    if iroh == "unpackaged":
        harness.iroh = None
    elif iroh == "unavailable":
        harness.iroh.available = False
    else:
        harness.store.active_iroh_room_id = None
    harness.controller.mode = harness.store.replication_mode = "iroh"
    harness.controller.restore_replication()
    state.online = True
    login_owner(cloud)
    assert cloud.authenticated and state.users == [OWNER]
    assert state.requests == [] and state.timers == []
    stream.assert_not_called()


@pytest.mark.parametrize("action", ["mode_change", "leave"])
def test_offline_join_cancel_persist_failure_keeps_route_retired(route, action):
    harness, cloud, state, stream = route
    harness.controller.mode = harness.store.replication_mode = "offline"
    harness.controller.restore_replication()
    state.online = True
    harness.controller._start_iroh_join(SimpleNamespace(room_id="joining-room"))
    failure = OSError("persist failed")
    harness.store.failure = failure
    if action == "leave":
        outcome = harness.controller.leave_iroh_room()
    else:
        outcome = harness.controller.replication_mode_changed("centralized")
    harness.store.failure = None
    assert outcome.effects[0].error is failure
    assert harness.controller.mode == harness.store.replication_mode == "offline"
    assert not harness.controller.iroh_join_pending
    assert state.requests == state.users == [] and state.timers == []
    assert not cloud.authenticated
    stream.assert_not_called()


@pytest.mark.parametrize("busy", [False, True])
def test_refused_iroh_selection_during_join_keeps_route_retired(route, busy):
    harness, cloud, state, stream = route
    start_join_from_live_cloud(harness, cloud, state, stream)
    requests = list(state.requests)
    cloud.busy = busy
    harness.controller.replication_mode_changed("iroh")
    cloud.busy = False
    assert harness.controller.iroh_join_pending
    assert harness.controller.mode == harness.store.replication_mode == "centralized"
    assert state.requests == requests and state.timers == []
    cloud.start_revision_stream()  # a later sync completion
    stream.assert_not_called()


@pytest.mark.parametrize("failure", [OSError("create failed"), ValueError("bad name")])
def test_failed_room_creation_keeps_cloud_stream(route, failure):
    harness, cloud, state, stream = route
    replies = open_live_stream(harness, cloud, state, stream)
    harness.store.failure = failure
    outcome = harness.controller.create_iroh_room("route test")
    harness.store.failure = None
    assert outcome.effects[0].error is failure
    assert harness.controller.mode == "centralized"
    replies[0].abort.assert_not_called()
    assert len(replies) == 1


@pytest.mark.parametrize("failure", [OSError("persist failed"), ValueError("bad mode")])
def test_plain_persist_failure_does_not_rerun_restore(route, failure):
    harness, cloud, state, stream = route
    replies = open_live_stream(harness, cloud, state, stream)
    requests = list(state.requests)
    harness.store.failure = failure
    outcome = harness.controller.replication_mode_changed("offline")
    harness.store.failure = None
    assert outcome.effects[0].error is failure
    assert harness.controller.mode == harness.store.replication_mode == "centralized"
    assert state.requests == requests and state.timers == []
    replies[0].abort.assert_not_called()
