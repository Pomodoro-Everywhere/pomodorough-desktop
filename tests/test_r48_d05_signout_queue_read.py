"""R48-D05: sign-out queue-count read must not escape its Qt slot."""

from __future__ import annotations

import os
import sqlite3
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from pomodorough.core import task_from_title
from pomodorough.shared_core import SharedCore, SharedCoreError
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore


@contextmanager
def _deny_meta_reads(store: Store):
    def authorize(action, arg1, _arg2, _db, _src):
        if action == sqlite3.SQLITE_READ and arg1 == "meta":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.connection.set_authorizer(authorize)
    try:
        yield
    finally:
        store.connection.set_authorizer(None)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _window(app: QApplication, tmp_path, **cloud_kwargs):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    cloud = FakeCloud(authenticated=True, **cloud_kwargs)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        view = MainWindow(store, cloud, QIcon(), locale="en")
    view.tick_timer.stop()
    view.sync_timer.stop()
    try:
        view.notice.disconnect()
    except RuntimeError:
        pass
    notices: list[str] = []
    view.notice.connect(notices.append)
    hooks: list[tuple[type, BaseException]] = []
    previous = sys.excepthook
    sys.excepthook = lambda exc_type, exc, _tb: hooks.append((exc_type, exc))
    try:
        yield view, store, cloud, notices, hooks
    finally:
        sys.excepthook = previous
        view.quitting = True
        view.close()
        view.deleteLater()
        app.processEvents()
        app.processEvents()
        store.close()


def _pump(app: QApplication) -> None:
    app.processEvents()
    app.processEvents()


def _assert_preserved(view, store, cloud, before_rows) -> None:
    assert cloud.authenticated is True
    assert cloud.logout_calls == 0
    assert not store.connection.in_transaction
    assert tuple(store.connection.iterdump()) == before_rows
    assert store.load() == store.load()


def test_sqlite_denial_via_signal_reports_once_with_notice(app, tmp_path) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        before = tuple(store.connection.iterdump())
        with (
            _deny_meta_reads(store),
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
            patch.object(QMessageBox, "question") as question,
            patch.object(QMessageBox, "warning"),
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_called_once()
        assert isinstance(capture.call_args.args[0], sqlite3.Error)
        assert notices == [str(capture.call_args.args[0])]
        question.assert_not_called()
        _assert_preserved(view, store, cloud, before)


def test_oserror_via_signal_reports_once_with_notice(app, tmp_path) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        with (
            patch.object(store, "load", side_effect=OSError("disk gone")),
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
            patch.object(QMessageBox, "question") as question,
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_called_once()
        assert isinstance(capture.call_args.args[0], OSError)
        assert notices == ["disk gone"]
        question.assert_not_called()
        assert cloud.logout_calls == 0
        assert not store.connection.in_transaction


def test_shared_core_error_via_signal_reports_once(app, tmp_path) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        with (
            patch.object(store, "load", side_effect=SharedCoreError("core down")),
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
            patch.object(QMessageBox, "question") as question,
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_called_once()
        assert notices == ["core down"]
        question.assert_not_called()
        assert cloud.logout_calls == 0


@pytest.mark.parametrize(
    "bad_state",
    [
        {"pending": None},
        {"pending": [], "pendingTasks": None},
        {},
    ],
)
def test_malformed_state_notice_only_without_capture(app, tmp_path, bad_state) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        before = tuple(store.connection.iterdump())
        with (
            patch.object(store, "load", return_value=dict(bad_state)),
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
            patch.object(QMessageBox, "question") as question,
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_not_called()
        assert len(notices) == 1
        question.assert_not_called()
        assert cloud.logout_calls == 0
        assert tuple(store.connection.iterdump()) == before


def test_success_yes_logs_out_with_queue_count(app, tmp_path) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        store.queue_task_operation(
            "upsert", task_from_title("Signout task"), now_ms=1_000
        )
        state = store.load()
        expected = sum(len(state[k]) for k in ("pending", "pendingTasks", "pendingDurations", "pendingAutoStarts", "pendingSelectedTasks"))
        assert expected >= 1
        seen: dict[str, str] = {}

        def fake_question(*args, **kwargs):
            seen["detail"] = args[2] if len(args) >= 3 else ""
            return QMessageBox.StandardButton.Yes

        with (
            patch.object(QMessageBox, "question", side_effect=fake_question),
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_not_called()
        assert cloud.logout_calls == 1
        assert notices == []
        assert str(expected) in seen["detail"]


def test_cancel_preserves_queues_without_logout(app, tmp_path) -> None:
    with _window(app, tmp_path) as (view, store, cloud, notices, hooks):
        store.queue_task_operation(
            "upsert", task_from_title("Keep me"), now_ms=1_000
        )
        before = tuple(store.connection.iterdump())
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as question,
            patch(
                "pomodorough.account_resolution_controller.capture_exception"
            ) as capture,
        ):
            view.account_button.click()
            _pump(app)
        assert hooks == []
        capture.assert_not_called()
        question.assert_called_once()
        assert cloud.logout_calls == 0
        assert notices == []
        assert tuple(store.connection.iterdump()) == before
