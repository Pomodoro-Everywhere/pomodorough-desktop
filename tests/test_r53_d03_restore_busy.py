"""R53-D03: a login worker must not consume a pending restore retry."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication
from test_r52_d03_restore_retry import _MemorySecretStore
from test_signout_cleanup_recovery import response

from pomodorough.network import ApiError, CloudService, TokenStore


@pytest.fixture
def interleaving(tmp_path):
    app = QApplication.instance() or QApplication([])
    state = SimpleNamespace(workers=[], timers=[], requests=[], users=[], online=False)

    def request(method, url, *args, **kwargs):
        state.requests.append(url)
        if not state.online:
            raise ApiError("synthetic offline startup")
        return response() if url.endswith("/refresh") else {"user": {"id": "stored"}}

    tokens = TokenStore("r53-retry", _MemorySecretStore(), tmp_path / "session.json")
    with (
        patch.object(QThreadPool.globalInstance(), "start", state.workers.append),
        patch.object(QTimer, "singleShot", lambda ms, cb: state.timers.append((ms, cb))),
    ):
        cloud = CloudService("r53-retry", "https://restore.test", token_store=tokens,
                             request=request)
        cloud._revocation_restore_timer.stop()
        cloud._sign_out_cleanup_timer.stop()
        cloud.signed_in.connect(state.users.append)
        cloud._accept_login_tokens(response())
        with patch.object(cloud, "start_revision_stream"):
            cloud.restore()
            state.workers.pop(0).run()
            assert len(state.requests) == 1
            assert cloud._restore_attempt == 1
            assert [ms for ms, _ in state.timers] == [1000]
            state.online = True
            try:
                yield cloud, state
            finally:
                cloud.shutdown()
    assert app is not None


def fire_retry(state):
    state.timers.pop(0)[1]()


def cancel_login(*args, **kwargs):
    raise ApiError("synthetic browser cancelled")


def start_busy_login(cloud, state):
    cloud.login()
    assert cloud.busy
    for _ in range(8):
        fire_retry(state)
        assert cloud._restore_attempt == 1
        assert len(state.requests) == 1
        assert len(state.workers) == 1
        assert len(state.timers) == 1


def test_cancelled_login_preserves_restore_retry(interleaving):
    cloud, state = interleaving
    with patch.object(cloud, "_authorize_google", side_effect=cancel_login):
        start_busy_login(cloud, state)
        state.workers.pop(0).run()
    assert not cloud.busy and not cloud.authenticated
    assert cloud.token_store.load() is not None
    fire_retry(state)
    assert len(state.workers) == 1
    state.workers.pop(0).run()
    assert cloud.authenticated and not cloud.busy
    assert state.users == [{"id": "stored"}]
    assert state.requests == [
        "https://restore.test/api/v1/me",
        "https://restore.test/api/v1/auth/refresh",
        "https://restore.test/api/v1/me",
    ]
    assert cloud._restore_attempt == 0
    assert state.timers == []


def test_successful_login_discards_deferred_restore(interleaving):
    cloud, state = interleaving
    with patch.object(cloud, "_authorize_google", return_value={"id": "login"}):
        start_busy_login(cloud, state)
        state.workers.pop(0).run()
    fire_retry(state)
    assert cloud.authenticated and not cloud.busy
    assert state.users == [{"id": "login"}]
    assert len(state.requests) == 1
    assert state.workers == state.timers == []


@pytest.mark.parametrize("guard", [
    "shutting_down", "sign_out_pending", "deleting_account", "authenticated",
    "generation", "epoch", "route",
])
@pytest.mark.parametrize("still_busy", [False, True])
def test_deferred_retry_rechecks_every_fence(interleaving, guard, still_busy):
    cloud, state = interleaving
    with patch.object(cloud, "_authorize_google", side_effect=cancel_login):
        start_busy_login(cloud, state)
        if not still_busy:
            state.workers.pop(0).run()
    if guard == "generation":
        cloud._state.account_generation += 1
    elif guard == "epoch":
        cloud._restore_epoch += 1
    elif guard == "route":
        cloud.api_base = "https://other.test"
    else:
        setattr(cloud._state, guard, True)
    fire_retry(state)
    assert len(state.requests) == 1
    if still_busy:
        with patch.object(cloud, "_authorize_google", side_effect=cancel_login):
            state.workers.pop(0).run()
    assert state.workers == state.timers == []
    assert state.users == []


@pytest.mark.parametrize("success", [False, True])
def test_stale_login_completion_cannot_revive_restore(interleaving, success):
    cloud, state = interleaving
    with patch.object(cloud, "_authorize_google", side_effect=(
        (lambda **kwargs: {"id": "stale"}) if success else cancel_login
    )):
        start_busy_login(cloud, state)
        cloud._state.account_generation += 1
        state.workers.pop(0).run()
    fire_retry(state)
    assert not cloud.authenticated and not cloud.busy
    assert state.users == []
    assert len(state.requests) == 1
    assert state.workers == state.timers == []
