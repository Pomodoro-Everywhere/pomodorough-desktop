"""R52-D01: denied account reset preserves auth and fences mutations.

Through the production account-button path with real Store/SQLite,
CloudService, TokenStore, MainWindow, and bundled SharedCore, SQLite fault
injection (COMMIT denial, DELETE denial on pending_commands) must keep the
account authenticated with credentials intact, leave SQL unchanged, fence
every mutation entry point, emit a storage notice, and retain the
authenticated workspace across reopen. Retry without fault completes
sign-out, clears tasks and credentials, lifts the fence, and reopens clean.
The no-fault control proves the scrub commits before credential removal.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("POMODOROUGH_SENTRY_DISABLE", "1")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from pomodorough.core import task_from_title
from pomodorough.network import CloudService, TokenStore
from pomodorough.shared_core import SharedCore
from pomodorough.sound import CompletionSound
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from test_signout_cleanup_recovery import (
    FileVault,
    fixture_environment,
    response,
)

API = "https://r52-d01-reset-fence.example.test"
OWNER = {"id": "r52-d01-owner", "email": "d01@example.invalid"}
PRIVATE = task_from_title("R52-D01 private task")


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    application = QApplication.instance() or QApplication([])
    QApplication.beep = staticmethod(lambda: None)  # type: ignore[method-assign]
    yield application


@contextmanager
def _blocked_workers():
    # CloudService farms restore/revocation/sync onto the global thread
    # pool. Letting the restore worker finish is a race: its synthetic
    # /me reply fails and flips authenticated False before the click.
    # Same pattern as test_signout_cleanup_recovery's recovery fixture.
    with patch.object(QThreadPool.globalInstance(), "start", lambda _w: None):
        yield


@contextmanager
def _quiet():
    with (
        patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False),
        patch.object(
            QSystemTrayIcon, "showMessage", side_effect=AssertionError("No notifications")
        ),
        patch.object(CompletionSound, "play", return_value=False),
    ):
        yield


def _seed(store: Store) -> None:
    store.set_user(OWNER)
    store.queue_task_operation("upsert", PRIVATE)
    store.sync_payload()


def _service(root: Path, store: Store, vault: FileVault):
    tokens = TokenStore(store.device_id, vault, root / "session.json")
    cloud = CloudService(
        store.device_id, API, token_store=tokens, request=lambda *_a, **_k: {}
    )
    cloud._revocation_restore_timer.stop()
    cloud._sign_out_cleanup_timer.stop()
    cloud._accept_login_tokens(response())
    cloud.authenticated = True
    return tokens, cloud


def _window(app: QApplication, store: Store, cloud: CloudService):
    window = MainWindow(store, cloud, QIcon(), locale="en")
    window.tick_timer.stop()
    window.sync_timer.stop()
    window.notice.disconnect(window._show_notice)
    notices: list[str] = []
    published: list[bool] = []
    window.notice.connect(notices.append)
    cloud.signed_out.connect(lambda: published.append(True))
    _process(app)
    return window, notices, published


def _process(app: QApplication, rounds: int = 10) -> None:
    for _ in range(rounds):
        app.processEvents()


def _dump(store: Store) -> str:
    return "\n".join(store.connection.iterdump())


def _deny(mode: str, denials: list):
    def authorize(action, first, *_a):
        denied = (mode == "delete" and action == sqlite3.SQLITE_DELETE
                  and first == "pending_commands") or (
            mode == "commit" and action == sqlite3.SQLITE_TRANSACTION
            and first == "COMMIT"
        )
        if denied:
            denials.append((action, first))
        return sqlite3.SQLITE_DENY if denied else sqlite3.SQLITE_OK

    return authorize


def _click_sign_out(window: MainWindow, arm=None) -> None:
    def confirm(*args):
        if arm is not None:
            arm()
        return QMessageBox.StandardButton.Yes

    with (
        patch.object(QMessageBox, "question", side_effect=confirm),
        patch.object(
            window, "_choose_resolution_account_action", return_value="sign_out"
        ),
        patch.object(window, "_choose_account_switch_action", return_value="sign_out"),
    ):
        window.account_button.click()


def _close(cloud: CloudService, window: MainWindow, store: Store) -> None:
    cloud.shutdown()
    window.quitting = True
    window.close()
    window.deleteLater()
    store.close()


def _reopen(root: Path):
    vault = FileVault(root)
    store = Store(root / "state.sqlite3", iroh_secret_store=vault)
    try:
        tokens = TokenStore(store.device_id, vault, root / "session.json")
        loaded = store.load(projection=True)
        return {
            "sql": _dump(store),
            "user": loaded["snapshot"]["user"],
            "tokens": tokens.load(),
        }
    finally:
        store.close()


def _denied_state(app: QApplication, tmp_path: Path, mode: str):
    """Seed, fault the reset through the account button, return evidence."""
    root = tmp_path / mode
    root.mkdir()
    vault = FileVault(root)
    store = Store(
        root / "state.sqlite3", shared_core=SharedCore(), iroh_secret_store=vault
    )
    _seed(store)
    tokens, cloud = _service(root, store, vault)
    window, notices, published = _window(app, store, cloud)
    errors: list[str] = []
    hook = sys.excepthook
    sys.excepthook = lambda kind, error, tb: errors.append(f"{kind.__name__}: {error}")
    before = (_dump(store), tokens.load())
    denials: list = []
    try:
        # Arm inside confirmation (after the queue-count read and dialog),
        # so the fault hits the reset scrub and nothing earlier.
        def arm() -> None:
            store.connection.set_authorizer(_deny(mode, denials))

        _click_sign_out(window, arm)
    finally:
        sys.excepthook = hook
        store.connection.set_authorizer(None)
    evidence = {
        "root": root, "store": store, "tokens": tokens, "cloud": cloud,
        "window": window, "notices": notices, "published": published,
        "errors": errors, "before_sql": before[0], "before_tokens": before[1],
        "after_sql": _dump(store),
    }
    assert denials, "reset fault injected during sign-out"
    return evidence


def _assert_fenced(evidence) -> None:
    window, store = evidence["window"], evidence["store"]
    window.task_input.setText("fenced must not insert")
    window._add_task()
    window._delete_task(PRIVATE["id"])
    window._issue("start")
    assert _dump(store) == evidence["before_sql"]


@pytest.mark.parametrize("mode", ["commit", "delete"])
def test_reset_denial_preserves_auth_and_fences(app, tmp_path, mode):
    with fixture_environment(tmp_path / mode):
        with _quiet(), _blocked_workers():
            evidence = _denied_state(app, tmp_path, mode)
    try:
        store, tokens = evidence["store"], evidence["tokens"]
        assert not evidence["errors"]
        assert not store.connection.in_transaction
        assert evidence["cloud"].authenticated is True
        assert tokens.load() == evidence["before_tokens"] != None  # noqa: E711
        assert evidence["after_sql"] == evidence["before_sql"]
        assert store.load(projection=True)["snapshot"]["user"] == OWNER
        assert any(t["id"] == PRIVATE["id"] for t in evidence["window"].tasks)
        assert evidence["notices"]
        assert not evidence["published"]
        assert evidence["window"]._history_resolution_active is True
        assert not evidence["window"].primary_button.isEnabled()
        assert not evidence["window"].add_task_button.isEnabled()
        assert evidence["window"].account_resolution._sign_out_phase == "reset"
        _assert_fenced(evidence)
    finally:
        _close(evidence["cloud"], evidence["window"], evidence["store"])


@pytest.mark.parametrize("mode", ["commit", "delete"])
def test_failed_reset_reopen_retains_workspace(app, tmp_path, mode):
    with fixture_environment(tmp_path / mode):
        with _quiet(), _blocked_workers():
            evidence = _denied_state(app, tmp_path, mode)
    try:
        fresh = _reopen(evidence["root"])
        assert fresh["sql"] == evidence["after_sql"]
        assert fresh["user"] == OWNER
        assert fresh["tokens"] == evidence["before_tokens"]
    finally:
        _close(evidence["cloud"], evidence["window"], evidence["store"])


@pytest.mark.parametrize("mode", ["commit", "delete"])
def test_retry_after_denial_signs_out_clean(app, tmp_path, mode):
    with fixture_environment(tmp_path / mode):
        with _quiet(), _blocked_workers():
            evidence = _denied_state(app, tmp_path, mode)
            window, store = evidence["window"], evidence["store"]
            _click_sign_out(window)
            _process(app)
            assert evidence["cloud"].authenticated is False
            assert evidence["tokens"].load() is None
            assert store.load(projection=True)["snapshot"]["user"] is None
            assert window.tasks == []
            assert window._history_resolution_active is False
            assert evidence["published"]
            root = evidence["root"]
            _close(evidence["cloud"], window, store)
            fresh = _reopen(root)
    assert fresh["user"] is None
    assert fresh["tokens"] is None


def test_signout_scrubs_before_credential_removal(app, tmp_path):
    root = tmp_path / "control"
    root.mkdir()
    with fixture_environment(root):
        with _quiet(), _blocked_workers():
            vault = FileVault(root)
            store = Store(
                root / "state.sqlite3", shared_core=SharedCore(),
                iroh_secret_store=vault,
            )
            _seed(store)
            tokens, cloud = _service(root, store, vault)
            window, _notices, published = _window(app, store, cloud)
            boundary: list[dict] = []
            real_clear = tokens.clear

            def observed_clear():
                loaded = store.load(projection=True)
                boundary.append({"user": loaded["snapshot"]["user"]})
                return real_clear()

            with patch.object(tokens, "clear", side_effect=observed_clear):
                _click_sign_out(window)
            _process(app)
            assert cloud.authenticated is False
            assert tokens.load() is None
            assert published
            assert boundary and boundary[0]["user"] is None
            _close(cloud, window, store)
            assert _reopen(root)["user"] is None
