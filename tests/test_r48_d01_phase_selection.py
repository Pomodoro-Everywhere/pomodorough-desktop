import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from pomodorough.core import format_remaining
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore


BOUNDARIES = ("begin", "settings", "version", "commit")


@pytest.fixture(scope="module")
def runtime():
    return QApplication.instance() or QApplication([]), SharedCore()


def seed_completed_focus(store):
    settings = store.load()["settings"]
    store.queue_command("start", None, "focus", settings["durationsMs"], now_ms=1_000)
    timer = store.projected_state(now_ms=1_000).canonical_timer
    store.queue_command("finish", timer, "focus", settings["durationsMs"], now_ms=2_000)


@pytest.fixture(params=("idle", "terminal"))
def phase_window(tmp_path, runtime, request):
    app, core = runtime
    path = tmp_path / "state.sqlite3"
    store = Store(path, shared_core=core, iroh_secret_store=MemorySecretStore())
    if request.param == "terminal":
        seed_completed_focus(store)
    store.set_selected_phase("long_break")
    cloud = FakeCloud(authenticated=False)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        view = MainWindow(store, cloud, QIcon(), locale="en")
    view.tick_timer.stop()
    view.sync_timer.stop()
    view.notice.disconnect()
    notices = []
    view.notice.connect(notices.append)
    assert view._current_timer()["status"] == (
        "completed" if request.param == "terminal" else "idle"
    )
    yield SimpleNamespace(
        store=store, view=view, path=path, core=core, cloud=cloud,
        notices=notices, status=request.param,
    )
    view.quitting = True
    view.close()
    view.deleteLater()
    app.processEvents()
    store.close()


def durable_rows(store):
    return tuple(store.connection.iterdump())


@contextmanager
def reject_phase_write(store, boundary):
    statements, transactions = [], []
    connection = store.connection
    if boundary in {"settings", "version"}:
        key = "settings" if boundary == "settings" else "selectedPhaseVersion"
        connection.execute(
            "CREATE TEMP TRIGGER reject_phase BEFORE UPDATE ON meta "
            f"WHEN NEW.key = '{key}' BEGIN "
            "SELECT RAISE(ABORT, 'phase persistence rejected'); END"
        )

    def authorize(action, operation, _argument, _database, _trigger):
        if action == sqlite3.SQLITE_TRANSACTION:
            transactions.append(operation)
            if operation == {"begin": "BEGIN", "commit": "COMMIT"}.get(boundary):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_trace_callback(statements.append)
    connection.set_authorizer(authorize)
    try:
        yield statements, transactions
    finally:
        connection.set_trace_callback(None)
        connection.set_authorizer(None)
        if boundary in {"settings", "version"}:
            connection.execute("DROP TRIGGER reject_phase")


def fail_selection(harness, boundary):
    before = durable_rows(harness.store)
    with (
        reject_phase_write(harness.store, boundary) as (statements, transactions),
        patch("pomodorough.timer_interaction_controller.capture_exception") as capture,
    ):
        harness.view.phase_buttons["short_break"].click()
    capture.assert_called_once()
    error = capture.call_args.args[0]
    assert isinstance(error, sqlite3.Error)
    assert harness.notices == [str(error)]
    assert not harness.store.connection.in_transaction
    assert durable_rows(harness.store) == before
    assert transactions == {
        "begin": ["BEGIN"], "settings": ["BEGIN", "ROLLBACK"],
        "version": ["BEGIN", "ROLLBACK"], "commit": ["BEGIN", "COMMIT", "ROLLBACK"],
    }[boundary]
    if boundary in {"version", "commit"}:
        assert any("INSERT INTO meta" in sql and "short_break" in sql for sql in statements)
    if boundary == "commit":
        assert any("INSERT INTO meta" in sql and "selectedPhaseVersion" in sql for sql in statements)
    return before


def assert_selected_presentation(view, phase):
    assert view._selected_phase() == phase
    assert view.settings["selectedPhase"] == phase
    assert {key for key, button in view.phase_buttons.items() if button.isChecked()} == {phase}
    assert view.clock.phase_text == view.strings.text(f"phase.{phase}").upper()
    assert view.clock.time_text == format_remaining(view.settings["durationsMs"][phase])
    assert view.clock.tick_count == view.settings["durationsMs"][phase] // 60_000
    assert view.primary_button.text() == view.strings.text(
        "status.primary.start", phase=view.strings.text(f"phase.{phase}").upper()
    )


def assert_reopened_selection(harness, rows, settings, version):
    with closing(Store(
        harness.path, shared_core=harness.core, iroh_secret_store=MemorySecretStore(),
    )) as reopened:
        terminal = LocalTimer(reopened)
        assert terminal.settings == settings
        assert terminal.selected_phase == settings["selectedPhase"]
        assert reopened.get_meta("selectedPhaseVersion") == version
        assert durable_rows(reopened) == rows


def assert_started_phase(harness, consumer, phase, version):
    before = harness.store.load()["pending"]
    notices = list(harness.notices)
    if consumer == "gui":
        harness.view.primary_button.click()
        timer = harness.view.timer
    elif consumer == "reopened_terminal":
        with closing(Store(harness.path, shared_core=harness.core)) as reopened:
            terminal = LocalTimer(reopened)
            terminal.primary()
            timer = terminal.timer
    else:
        terminal = LocalTimer(harness.store)
        terminal.primary()
        timer = terminal.timer
    assert timer["status"] == "running"
    assert timer["phase"] == phase
    assert timer["plannedDurationMs"] == harness.view.settings["durationsMs"][phase]
    state = harness.store.load()
    assert state["pending"][:len(before)] == before
    added = state["pending"][len(before):]
    assert [command["type"] for command in added] == (
        ["clear", "start"] if harness.status == "terminal" else ["start"]
    )
    assert added[-1]["phase"] == phase
    assert added[-1]["timerId"] == timer["id"]
    assert harness.notices == notices
    assert_reopened_selection(harness, durable_rows(harness.store), state["settings"], version)
    with closing(Store(harness.path, shared_core=harness.core)) as reopened:
        assert reopened.load()["pending"] == state["pending"]
        assert LocalTimer(reopened).timer == timer


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_sqlite_phase_failure_preserves_presentation_and_reopen(phase_window, boundary):
    harness = phase_window
    settings = deepcopy(harness.view.settings)
    version = harness.store.get_meta("selectedPhaseVersion")
    before = fail_selection(harness, boundary)
    assert harness.view.settings == settings
    assert_selected_presentation(harness.view, "long_break")
    assert_reopened_selection(harness, before, settings, version)
    assert harness.cloud.payloads == []


@pytest.mark.parametrize("consumer", ("gui", "terminal", "reopened_terminal"))
@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_sqlite_phase_failure_next_start_uses_committed_selection(
    phase_window, boundary, consumer,
):
    harness = phase_window
    version = harness.store.get_meta("selectedPhaseVersion")
    fail_selection(harness, boundary)
    assert_started_phase(harness, consumer, "long_break", version)


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_valid_selection_retry_commits_once_after_sqlite_failure(phase_window, boundary):
    harness = phase_window
    version = harness.store.get_meta("selectedPhaseVersion")
    fail_selection(harness, boundary)
    harness.notices.clear()
    harness.view.phase_buttons["short_break"].click()
    assert harness.view.settings == harness.store.load()["settings"]
    assert_selected_presentation(harness.view, "short_break")
    assert harness.store.get_meta("selectedPhaseVersion") == version + 1
    assert harness.notices == []
    assert_started_phase(harness, "gui", "short_break", version + 1)


def test_invalid_phase_keeps_committed_selection_and_next_start(phase_window):
    harness = phase_window
    settings = deepcopy(harness.view.settings)
    before = durable_rows(harness.store)
    version = harness.store.get_meta("selectedPhaseVersion")
    with patch("pomodorough.timer_interaction_controller.capture_exception") as capture:
        harness.view._select_phase("unsupported")
    capture.assert_not_called()
    assert harness.notices == ["Unsupported timer phase."]
    assert durable_rows(harness.store) == before
    assert harness.view.settings == settings
    assert_selected_presentation(harness.view, "long_break")
    assert_reopened_selection(harness, before, settings, version)
    assert_started_phase(harness, "gui", "long_break", version)


@pytest.mark.parametrize("phase", ("focus", "short_break", "long_break"))
def test_successful_phase_selection_publishes_after_commit_and_increments_version(
    phase_window, phase,
):
    harness = phase_window
    settings = deepcopy(harness.view.settings)
    version = harness.store.get_meta("selectedPhaseVersion")
    commit_selections = []

    def observe(sql):
        if sql == "COMMIT":
            commit_selections.append(harness.view.settings["selectedPhase"])

    harness.store.connection.set_trace_callback(observe)
    try:
        harness.view.phase_buttons[phase].click()
    finally:
        harness.store.connection.set_trace_callback(None)
    assert commit_selections == [settings["selectedPhase"]]
    settings["selectedPhase"] = phase
    assert harness.view.settings == harness.store.load()["settings"] == settings
    assert harness.store.get_meta("selectedPhaseVersion") == version + 1
    assert harness.notices == []
    assert_selected_presentation(harness.view, phase)
    assert_reopened_selection(harness, durable_rows(harness.store), settings, version + 1)
    assert_started_phase(harness, "gui", phase, version + 1)
