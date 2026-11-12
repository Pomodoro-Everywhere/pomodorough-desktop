"""UX round 2 B2 rule 7: a pending task deletion is durable.

The undo window has no timeout, so the delete is recorded in the local store
before the row hides. Undo withdraws it; a marker that names another
workspace, a missing task or nothing readable deletes nothing. A marker a
dead process left deletes nothing silently either: the next start keeps the
task and asks, and only Retry there deletes it. The marker cannot tell a
killed run from an Undo whose marker clear failed until shutdown.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import test_storage
from PySide6.QtWidgets import QApplication, QMessageBox

from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp
from pomodorough.tasks_screen import TasksScreen
from test_r2_a1_task_undo_window import _delete_row, _pending_delete, _populate
from test_r2_a2_task_failure_notice import CAPTURE, RAW_DETAIL, _Dialogs, _dialogs, _failing_store
from test_r52_d06_task_header_wrap import _process, _tasks_window
from test_ui import MemorySecretStore

MARKER_KEY = "pendingTaskDeletion"
QUESTION = "pomodorough.account_resolution_controller.QMessageBox.question"
ACCOUNT = {"id": "account-a", "email": "a@example.invalid"}


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@contextmanager
def _stored(tmp_path: Path) -> Iterator[Store]:
    """Open the profile between two runs, as the next process would."""
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    try:
        yield store
    finally:
        store.close()


def _die_during_the_undo_window(window) -> None:
    """Stop every completion path, so closing acts like a killed process."""
    window.tasks_screen.flush_pending_delete = lambda: None
    window.complete_pending_task_delete_unattended = lambda: None


def _queued_deletes(store: Store) -> list[str]:
    return [
        operation["taskId"]
        for operation in store.load()["pendingTasks"]
        if operation["type"] == "delete"
    ]


def _queued_changes(store: Store) -> int:
    # The same queues the sign-out question counts.
    state = store.load()
    keys = ("pending", "pendingTasks", "pendingDurations", "pendingAutoStarts",
            "pendingSelectedTasks")
    return sum(len(state[key]) for key in keys)


def _task_ids(window) -> list[str]:
    return [task["id"] for task in window.tasks]


def _run_with_two_tasks(app, tmp_path: Path) -> list[str]:
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _populate(app, window, 2)
        return _task_ids(window)


# --- Process death and restart -------------------------------------------------


def _next_start_asks(app, tmp_path, task_id: str, answer: str) -> list[str]:
    """Start again; the recorded delete is put to the user, never applied."""
    strings = Strings("en")
    with _dialogs("en", answer) as dialogs:
        with _tasks_window(app, tmp_path, "en", 640, 360) as window:
            _process(app, 20)
            assert window.store.get_meta(MARKER_KEY) is None
            queued = _queued_deletes(window.store)
            task_ids = _task_ids(window)
    assert dialogs.shown == [
        (strings.text("task.error.delete_unfinished"), ["Retry", "Cancel"])
    ]
    assert (task_id in task_ids) is (answer == "task.error.cancel")
    return queued


@pytest.mark.parametrize("answer", ["task.error.retry", "task.error.cancel"])
def test_a_killed_process_asks_about_the_delete_at_next_start(app, tmp_path, answer):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        _die_during_the_undo_window(window)
    with _stored(tmp_path) as store:
        # The first run never completed the delete; only the marker is left.
        assert _queued_deletes(store) == []
        assert store.get_meta(MARKER_KEY)["taskId"] == task_id
    queued = _next_start_asks(app, tmp_path, task_id, answer)
    assert queued == ([task_id] if answer == "task.error.retry" else [])


def test_undo_before_a_kill_keeps_the_task(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window.tasks_screen.undo_button.click()
        _process(app)
        assert window.store.get_meta(MARKER_KEY) is None
        _die_during_the_undo_window(window)
    assert requested == []
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert task_id in _task_ids(window)


def _delete_queued_then_killed(app, tmp_path: Path) -> tuple[str, list[dict[str, str]]]:
    """Leave the profile of a run that died after its delete was queued.

    The delete is queued in one store transaction and its marker is cleared
    in the next one, so a kill between the two leaves both. Here the delete
    completes, and the marker is then written back as the dead run left it.
    """
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        marker = window.store.get_meta(MARKER_KEY)
        window.tasks_screen.undo_banner.dismiss_button.click()
        _process(app)
        assert requested == [task_id]
        kept = [dict(task) for task in window.tasks]
    with _stored(tmp_path) as store:
        assert _queued_deletes(store) == [task_id]
        store.set_meta(MARKER_KEY, marker)
    return task_id, kept


def _acknowledge_everything(tmp_path: Path, tasks: list[dict[str, str]]) -> None:
    """Sync until the server has applied every queued operation."""
    with _stored(tmp_path) as store:
        for revision in range(1, 11):
            if not _queued_changes(store):
                return
            request = store.sync_payload()
            # A module import: a TestCase class in this namespace would run here.
            response = test_storage.StorageTests._canonical_response(
                request, revision=revision, tasks=tasks
            )
            # A live server clock keeps retained operations in trusted time.
            server_wall = int(time.time() * 1000)
            response["serverHlcWallMs"] = server_wall
            response["serverTime"] = utc_timestamp(server_wall)
            store.apply_sync(response, request)
        assert _queued_changes(store) == 0


@pytest.mark.parametrize("server", ["queued", "acknowledged"])
def test_a_marker_left_after_the_delete_was_queued_keeps_it_deleted(
    app, tmp_path, server
):
    # The marker is written before the delete is queued and cleared after,
    # so a leftover marker can name a delete that is queued or already
    # applied by the server. The task is then gone from the projection; the
    # start drops the marker, asks nothing and brings nothing back.
    task_id, kept = _delete_queued_then_killed(app, tmp_path)
    if server == "acknowledged":
        _acknowledge_everything(tmp_path, kept)
    with _dialogs("en", "task.error.retry") as dialogs:
        with _tasks_window(app, tmp_path, "en", 640, 360) as window:
            _process(app, 20)
            assert window.store.get_meta(MARKER_KEY) is None
            assert task_id not in _task_ids(window)
            queued = _queued_deletes(window.store)
    assert dialogs.shown == []
    assert queued == ([task_id] if server == "queued" else [])


@pytest.mark.parametrize(
    "marker",
    [
        # Another workspace owner recorded it.
        {"version": 1, "taskId": None, "owner": {"kind": "account", "id": "someone-else"}},
        # The task no longer exists.
        {"version": 1, "taskId": "missing-task", "owner": {"kind": "local"}},
        # Nothing readable.
        {"version": 99, "taskId": None, "owner": {"kind": "local"}},
        "not a marker",
    ],
)
def test_a_foreign_or_unreadable_marker_deletes_nothing(app, tmp_path, marker):
    task_ids = _run_with_two_tasks(app, tmp_path)
    if isinstance(marker, dict) and marker["taskId"] is None:
        marker = {**marker, "taskId": task_ids[0]}
    with _stored(tmp_path) as store:
        store.set_meta(MARKER_KEY, marker)
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert _task_ids(window) == task_ids
        assert window.store.get_meta(MARKER_KEY) is None


def test_a_delete_recorded_before_an_owner_change_is_dropped(app, tmp_path):
    # M1: task ids derive from titles, so the same id can name another
    # workspace's task once the workspace changed hands. Here the account
    # is signed out from elsewhere during the undo window, so no account
    # action completes the delete first, and the local workspace then gets
    # a task with the same title.
    with _stored(tmp_path) as store:
        store.set_user(ACCOUNT)
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        title = next(task["title"] for task in window.tasks if task["id"] == task_id)
        window.cloud.signed_out.emit()
        _process(app)
        assert window.user is None
        assert task_id not in _task_ids(window)
        window._add_task(title)
        _process(app, 20)
        assert task_id in _task_ids(window)
        window.tasks_screen.undo_banner.dismiss_button.click()
        _process(app)
        assert requested == [task_id]
        assert task_id in _task_ids(window)
        assert _queued_deletes(window.store) == []
        assert window.store.get_meta(MARKER_KEY) is None


# --- Sign-out and other non-screen entry points ----------------------------------


def test_sign_out_completes_the_delete_and_counts_it(app, tmp_path):
    # M1: the delete completes before sign-out can reset the workspace, and
    # the sign-out question counts it with the other queued changes.
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        queued = _queued_changes(window.store)
        asked: list[str] = []

        def question(_parent, _title, detail, *_rest):
            asked.append(detail)
            return QMessageBox.StandardButton.Cancel

        with patch(QUESTION, question):
            window._account_action()
        _process(app)
        strings = Strings("en")
        label = strings.plural("queue.changes", queued + 1)
        assert asked == [strings.text("account.signout_detail", queue=label)]
        assert requested == [task_id]
        assert window.tasks_screen.pending_delete_task_id() is None
        assert not window.tasks_screen.undo_banner.isVisible()
        assert task_id not in _task_ids(window)


def test_sign_out_during_the_window_then_sign_in_keeps_the_task_deleted(app, tmp_path):
    # Rule 7. Sign-out completes the delete before it resets the workspace;
    # after signing in again nothing local brings the task back or deletes
    # anything later. Whether the server still has the task is the sign-out
    # discard limitation (lead answer 3), which this test does not cover.
    with _stored(tmp_path) as store:
        store.set_user(ACCOUNT)
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        with patch(QUESTION, return_value=QMessageBox.StandardButton.Yes):
            window._account_action()
        _process(app)
        assert requested == [task_id]
        assert window.cloud.logout_calls == 1
        window.cloud.signed_out.emit()
        _process(app)
        assert window.store.get_meta(MARKER_KEY) is None
        window.cloud.signed_in.emit(ACCOUNT)
        _process(app)
        assert task_id not in _task_ids(window)
        assert window.tasks_screen.pending_delete_task_id() is None
        assert window.store.get_meta(MARKER_KEY) is None
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert task_id not in _task_ids(window)
        assert window.store.get_meta(MARKER_KEY) is None


@pytest.mark.parametrize("entry", ["tray", "space", "finish", "timer_screen"])
def test_every_user_timer_entry_point_completes_the_delete(app, tmp_path, entry):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        if entry == "finish":
            # Finish needs a running timer; starting it first is unrelated.
            window._issue("start")
            _process(app)
        task_id, requested = _pending_delete(app, window)
        focused = QApplication.focusWidget()
        if focused is not None:
            # D71: a focused button keeps Space for itself.
            focused.clearFocus()
        trigger = {
            "tray": window._primary_action,  # tray_primary.triggered
            "space": window.shortcuts[0].activated.emit,
            "finish": window.shortcuts[1].activated.emit,
            "timer_screen": window.timer_screen.primary_action_requested.emit,
        }[entry]
        trigger()
        _process(app)
        assert requested == [task_id]
        assert window.tasks_screen.pending_delete_task_id() is None
        assert task_id not in _task_ids(window)


# --- Failures with no window to ask in (M3) -----------------------------------------


def test_a_failed_delete_on_hide_asks_once_the_window_is_back(app, tmp_path):
    strings = Strings("en")
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        with (
            _dialogs("en", "task.error.retry") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL), failures=1),
            patch(CAPTURE),
        ):
            window.hide()
            _process(app)
            # No Retry modal on a window that is going away.
            assert dialogs.shown == []
            assert task_id in _task_ids(window)
            window.show()
            _process(app, 20)
        assert dialogs.shown == [
            (strings.text("task.error.delete_unfinished"), ["Retry", "Cancel"])
        ]
        assert task_id not in _task_ids(window)
        assert window.store.get_meta(MARKER_KEY) is None


def test_a_failed_delete_at_quit_is_put_to_the_user_at_next_start(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        with (
            _dialogs("en") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL)),
            patch(CAPTURE),
        ):
            window.quitting = True
            window.shutdown()
            _process(app)
        assert dialogs.shown == []
    assert _next_start_asks(app, tmp_path, task_id, "task.error.retry") == [task_id]


# --- L1: one dialog at a time, nothing dropped ----------------------------------------


@pytest.mark.parametrize(
    ("error", "key", "buttons"),
    [
        (OSError(RAW_DETAIL), "task.error.add_failed", ["Retry", "Cancel"]),
        (ValueError(RAW_DETAIL), "task.error.change_rejected", []),
    ],
)
def test_a_task_failure_behind_an_open_notice_waits_for_it(app, tmp_path, error, key, buttons):
    strings = Strings("en")
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        dialogs = _Dialogs(strings, ("task.error.cancel",))

        def while_the_first_dialog_is_open() -> None:
            if not dialogs.shown:
                # While the first notice is open, a task change fails.
                with _failing_store(window, error), patch(CAPTURE):
                    window._add_task("Behind a notice")

        def exec_dialog(dialog: QMessageBox) -> int:
            while_the_first_dialog_is_open()
            return dialogs.exec(dialog)

        def warning(*args):
            while_the_first_dialog_is_open()
            return dialogs.warning(*args)

        with (
            patch.object(QMessageBox, "exec", exec_dialog),
            patch.object(QMessageBox, "warning", warning),
        ):
            window._show_notice("Another notice.")
            _process(app)
        assert dialogs.shown == [("Another notice.", []), (strings.text(key), buttons)]


# --- L7: a sync that arrives during the undo window ----------------------------------


@pytest.fixture
def screen(app):
    view = TasksScreen(Strings("en"))
    view.resize(480, 360)
    view.show()
    view.requested = []
    view.delete_task_requested.connect(view.requested.append)
    yield view
    view.close()
    _process(app)
    view.deleteLater()


def _tasks(*titles: str) -> list[dict]:
    return [task_from_title(title) for title in titles]


def test_a_remote_rename_during_the_window_shows_the_new_row(app, screen):
    screen.render(_tasks("Alpha", "Beta"), [], mutations_enabled=True)
    _process(app)
    deleted = _delete_row(app, screen.task_table, 0)
    banner_text = screen.undo_banner.label.text()
    # Another device renamed Alpha: the new identity is a different task.
    screen.render(_tasks("Alpha renamed", "Beta"), [], mutations_enabled=True)
    _process(app)
    shown = [
        screen.task_table.cellWidget(row, 3).property("taskId")
        for row in range(screen.task_table.rowCount())
    ]
    assert shown == [task["id"] for task in _tasks("Alpha renamed", "Beta")]
    # The banner still names the task the user deleted, and Dismiss deletes it.
    assert screen.undo_banner.label.text() == banner_text
    screen.undo_banner.dismiss_button.click()
    _process(app)
    assert screen.requested == [deleted]


def test_a_remote_re_add_of_the_same_title_stays_hidden(app, screen):
    screen.render(_tasks("Alpha", "Beta"), [], mutations_enabled=True)
    _process(app)
    deleted = _delete_row(app, screen.task_table, 0)
    screen.render(_tasks("Beta", "Alpha"), [], mutations_enabled=True)
    _process(app)
    shown = [
        screen.task_table.cellWidget(row, 3).property("taskId")
        for row in range(screen.task_table.rowCount())
    ]
    # The same title is the same task id, so it stays in the undo window.
    assert deleted not in shown
    assert screen.pending_delete_task_id() == deleted
