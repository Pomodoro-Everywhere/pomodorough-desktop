"""UX round 2 B2 rework: a delete the user saw fail is never applied later.

Batch B acceptance found that a pending delete outlived the notice that told
the user it had not happened (H1, M1, M2). The rule, shared with the other
clients: a deleted task never comes back silently and is never deleted
silently later. A failure or refusal shown with the window in view ends the
pending delete and clears its marker; only Retry in that notice, which keeps
the task id, can still delete the task. Without a window in view the marker
stays and the user is told once the window is back (rule 6).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from pomodorough.failure_notice import describe_failure
from pomodorough.localization import Strings
from pomodorough.pending_task_deletion import record_pending_task_deletion
from pomodorough.storage import Store
from pomodorough.timer_screen import TimerScreen
from test_r2_a1_task_undo_window import _delete_row, _pending_delete, _populate
from test_r2_a2_task_failure_notice import CAPTURE, RAW_DETAIL, _dialogs, _failing_store
from test_r2_b2_pending_delete_durable import (
    MARKER_KEY,
    QUESTION,
    _queued_changes,
    _queued_deletes,
    _stored,
    _task_ids,
)
from test_r52_d05_nav_reflow import _scaled_font
from test_r52_d06_task_header_wrap import _process, _tasks_window
from test_ux_timer_controls import SETTINGS, _render

STRINGS = Strings("en")


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _start(app, window) -> None:
    """Press Start the way Space and the tray do."""
    focused = QApplication.focusWidget()
    if focused is not None:
        # D71: a focused button keeps Space for itself.
        focused.clearFocus()
    window._primary_action()
    _process(app)


def _timer_status(window) -> str | None:
    return window.timer.get("status")


def _cancel_a_failed_delete_now(app, window) -> str:
    """DELETE NOW fails once; the user reads the notice and presses Cancel."""
    task_id, _requested = _pending_delete(app, window)
    with (
        _dialogs("en", "task.error.cancel") as dialogs,
        _failing_store(window, OSError(RAW_DETAIL), failures=1),
        patch(CAPTURE),
    ):
        window.tasks_screen.undo_banner.dismiss_button.click()
        _process(app)
    assert dialogs.shown == [(STRINGS.text("task.error.delete_failed"), ["Retry", "Cancel"])]
    assert task_id in _task_ids(window)
    return task_id


# --- H1: Cancel on a shown failure ends the delete ------------------------------


def test_a_cancelled_failed_delete_is_not_applied_by_start(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id = _cancel_a_failed_delete_now(app, window)
        assert window.store.get_meta(MARKER_KEY) is None
        with _dialogs("en") as dialogs:
            _start(app, window)
        assert dialogs.shown == []
        assert task_id in _task_ids(window), "Start silently deleted the task"
        assert _queued_deletes(window.store) == []
        assert _timer_status(window) == "running"


def test_a_cancelled_failed_delete_is_not_replayed_at_next_start(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id = _cancel_a_failed_delete_now(app, window)
    with _stored(tmp_path) as store:
        assert store.get_meta(MARKER_KEY) is None
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert task_id in _task_ids(window), "the next start silently deleted the task"
        assert _queued_deletes(window.store) == []


def test_retry_on_a_shown_failure_still_deletes(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        with (
            _dialogs("en", "task.error.retry") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL), failures=1),
            patch(CAPTURE),
        ):
            window.tasks_screen.undo_banner.dismiss_button.click()
            _process(app)
        assert dialogs.shown == [(STRINGS.text("task.error.delete_failed"), ["Retry", "Cancel"])]
        assert task_id not in _task_ids(window)
        assert _queued_deletes(window.store) == [task_id]
        assert window.store.get_meta(MARKER_KEY) is None


def test_cancel_on_the_unfinished_notice_withdraws_the_delete(app, tmp_path):
    # Rule 6 path: the delete failed while the window hid, so the user hears
    # of it once the window is back. Cancel there ends it like any Cancel.
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        with (
            _dialogs("en", "task.error.cancel") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL), failures=1),
            patch(CAPTURE),
        ):
            window.hide()
            _process(app)
            assert dialogs.shown == []
            assert window.store.get_meta(MARKER_KEY)["taskId"] == task_id
            window.show()
            _process(app, 20)
        assert dialogs.shown == [
            (STRINGS.text("task.error.delete_unfinished"), ["Retry", "Cancel"])
        ]
        assert window.store.get_meta(MARKER_KEY) is None
        with _dialogs("en") as later:
            _start(app, window)
        assert later.shown == []
        assert task_id in _task_ids(window), "Start deleted a task the user kept"
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert task_id in _task_ids(window)
        assert _queued_deletes(window.store) == []


# --- M1: a rejected delete ends; Start runs the selected task --------------------


@pytest.mark.parametrize("store_after", ["recovers", "keeps_rejecting"])
def test_a_rejected_delete_lets_start_run_the_selected_task(app, tmp_path, store_after):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _populate(app, window, 2)
        task_id = window.task_table.cellWidget(0, 3).property("taskId")
        window.task_combo.setCurrentIndex(window.task_combo.findData(task_id))
        _process(app)
        assert window.settings["selectedTaskId"] == task_id
        assert _delete_row(app, window.task_table, 0) == task_id
        with _dialogs("en") as dialogs, ExitStack() as rejecting:
            rejecting.enter_context(_failing_store(window, ValueError(RAW_DETAIL)))
            window.tasks_screen.undo_banner.dismiss_button.click()
            _process(app)
            assert dialogs.shown == [(STRINGS.text("task.error.delete_rejected"), [])]
            if store_after == "recovers":
                rejecting.close()
            _start(app, window)
        # One notice for one refusal; Start neither retries nor deletes.
        assert dialogs.shown == [(STRINGS.text("task.error.delete_rejected"), [])]
        assert task_id in _task_ids(window)
        assert window.store.get_meta(MARKER_KEY) is None
        assert _timer_status(window) == "running"
        assert window.settings["selectedTaskId"] == task_id


# --- Refused while changes are blocked (H1 attended, L1 unattended) ----------------


def test_a_delete_refused_in_view_ends_with_the_block_notice(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, requested = _pending_delete(app, window)
        window.replication.iroh_join_pending = True
        with _dialogs("en") as dialogs:
            window.tasks_screen.undo_banner.dismiss_button.click()
            _process(app)
        assert dialogs.shown == [(STRINGS.text("network.wait_join"), [])]
        assert requested == [task_id]
        assert task_id in _task_ids(window)
        assert window.store.get_meta(MARKER_KEY) is None
        window.replication.iroh_join_pending = False
        with _dialogs("en") as later:
            _start(app, window)
        assert later.shown == []
        assert task_id in _task_ids(window), "Start applied a refused delete"


def test_a_delete_refused_while_hiding_opens_no_modal_and_asks_later(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        window.replication.iroh_join_pending = True
        with _dialogs("en", "task.error.retry") as dialogs:
            window.hide()
            _process(app)
            # L1: no modal on a window that is going away.
            assert dialogs.shown == []
            assert window.store.get_meta(MARKER_KEY)["taskId"] == task_id
            window.replication.iroh_join_pending = False
            window.show()
            _process(app, 20)
        assert dialogs.shown == [
            (STRINGS.text("task.error.delete_unfinished"), ["Retry", "Cancel"])
        ]
        assert task_id not in _task_ids(window)
        assert window.store.get_meta(MARKER_KEY) is None


# --- M2: Undo whose marker clear failed --------------------------------------------


def _undo_while_the_marker_clear_fails(app, window) -> str:
    task_id, requested = _pending_delete(app, window)
    real = window.store.set_meta

    def failing(key, value):
        if key == MARKER_KEY and value is None:
            raise OSError(RAW_DETAIL)
        return real(key, value)

    with (
        _dialogs("en", "task.error.cancel") as dialogs,
        patch.object(window.store, "set_meta", failing),
        patch(CAPTURE),
    ):
        window.tasks_screen.undo_button.click()
        _process(app)
    assert dialogs.shown == [(STRINGS.text("failure.storage"), ["Retry", "Cancel"])]
    assert requested == []
    assert task_id in _task_ids(window)
    # The clear is still due; the store kept the marker.
    assert window.store.get_meta(MARKER_KEY)["taskId"] == task_id
    return task_id


def test_an_undo_whose_marker_clear_failed_is_not_replayed(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id = _undo_while_the_marker_clear_fails(app, window)
    with _stored(tmp_path) as store:
        assert store.get_meta(MARKER_KEY) is None
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        assert task_id in _task_ids(window), "the next start deleted an undone task"


def _undo_then_quit_while_the_marker_clear_fails(app, tmp_path) -> str:
    """Undo, then quit, with every clear of the marker failing."""
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        task_id, _requested = _pending_delete(app, window)
        real = window.store.set_meta

        def failing(key, value):
            if key == MARKER_KEY and value is None:
                raise OSError(RAW_DETAIL)
            return real(key, value)

        with (
            _dialogs("en", "task.error.cancel"),
            patch.object(window.store, "set_meta", failing),
            patch(CAPTURE),
        ):
            window.tasks_screen.undo_button.click()
            _process(app)
            window.quitting = True
            window.shutdown()
            _process(app)
    return task_id


def test_a_failed_marker_clear_at_shutdown_is_logged(app, tmp_path, caplog):
    with caplog.at_level("WARNING", logger="pomodorough.timer_interaction_controller"):
        _undo_then_quit_while_the_marker_clear_fails(app, tmp_path)
    logged = [record.getMessage() for record in caplog.records]
    assert any(
        "marker could not be cleared" in message and RAW_DETAIL in message
        for message in logged
    ), logged


def test_a_marker_left_by_a_failed_clear_keeps_the_task_and_says_so(app, tmp_path):
    task_id = _undo_then_quit_while_the_marker_clear_fails(app, tmp_path)
    with _stored(tmp_path) as store:
        # Process death would leave the same marker; nothing tells them apart.
        assert store.get_meta(MARKER_KEY)["taskId"] == task_id
    with _dialogs("en", "task.error.cancel") as dialogs:
        with _tasks_window(app, tmp_path, "en", 640, 360) as window:
            _process(app, 20)
            assert task_id in _task_ids(window), "the next start deleted a restored task"
            assert _queued_deletes(window.store) == []
            assert window.store.get_meta(MARKER_KEY) is None
    assert dialogs.shown == [
        (STRINGS.text("task.error.delete_unfinished"), ["Retry", "Cancel"])
    ]


def test_a_marker_that_cannot_be_dropped_at_start_is_reported(app, tmp_path):
    task_id = _undo_then_quit_while_the_marker_clear_fails(app, tmp_path)
    real = Store.set_meta

    def failing(store, key, value):
        if key == MARKER_KEY and value is None:
            raise OSError(RAW_DETAIL)
        return real(store, key, value)

    with (
        _dialogs("en", "task.error.cancel") as dialogs,
        patch.object(Store, "set_meta", failing),
        patch(CAPTURE) as capture,
    ):
        with _tasks_window(app, tmp_path, "en", 640, 360) as window:
            _process(app, 20)
            assert task_id in _task_ids(window), "the next start deleted a restored task"
            assert _queued_deletes(window.store) == []
    # The marker stays, so the start after this one asks again.
    with _stored(tmp_path) as store:
        assert store.get_meta(MARKER_KEY)["taskId"] == task_id
    # Once at start and once more at shutdown, the next retry point.
    assert [str(call.args[0]) for call in capture.call_args_list] == [RAW_DETAIL] * 2
    stored_failure = describe_failure(STRINGS, OSError(RAW_DETAIL)).sentence
    assert RAW_DETAIL not in stored_failure
    assert [sentence for sentence, _buttons in dialogs.shown] == [
        stored_failure,
        STRINGS.text("task.error.delete_unfinished"),
    ]


def test_sign_out_does_not_count_an_undone_delete(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 640, 360) as window:
        _undo_while_the_marker_clear_fails(app, window)
        queued = _queued_changes(window.store)
        asked: list[str] = []

        def question(_parent, _title, detail, *_rest):
            asked.append(detail)
            return QMessageBox.StandardButton.Cancel

        with patch(QUESTION, question):
            window._account_action()
        _process(app)
        label = STRINGS.plural("queue.changes", queued)
        assert asked == [STRINGS.text("account.signout_detail", queue=label)]
        assert window.store.get_meta(MARKER_KEY) is None


# --- L2: a reset clears the marker for the same owner -------------------------------


def test_a_same_owner_reset_clears_the_marker(app, tmp_path):
    with _stored(tmp_path) as store:
        # The local workspace keeps its owner across a reset.
        record_pending_task_deletion(store, "task-a", None)
        store.reset_account_data()
        assert store.get_meta(MARKER_KEY) is None


# --- L3: the Timer controls show a scroll bar on overflow ---------------------------


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_timer_controls_show_a_scroll_bar_when_they_overflow(app, locale):
    with _scaled_font(app, 30.0):
        screen = TimerScreen(Strings(locale), SETTINGS)
        _render(screen, "idle", skip_destination="short_break")
        screen.resize(screen.minimumSizeHint())
        screen.show()
        _process(app, 12)
        try:
            scroll = screen.controls_scroll
            column = screen.controls_column
            assert column.height() > scroll.viewport().height()
            # A pointer without a wheel can reach the secondary row.
            assert scroll.verticalScrollBar().isVisible()
            # The shown bar never narrows the column below its minimum.
            assert column.width() >= column.minimumSizeHint().width()
            assert scroll.horizontalScrollBar().maximum() == 0
        finally:
            screen.close()
            screen.deleteLater()
            _process(app)
