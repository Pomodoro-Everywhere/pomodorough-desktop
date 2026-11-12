"""Deferred Tasks focus reveals belong to the receiving widget's lifetime."""

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QScrollArea
from shiboken6 import delete, isValid

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen

CHILD = Path(__file__).with_name("r54_d03_exec_child.py")
TASKS_SCREEN = Path(inspect.getfile(TasksScreen)).resolve()


class ObservedTasksScreen(TasksScreen):
    def __init__(self, deliveries):
        self.deliveries = deliveries
        super().__init__(Strings("en"))

    def _reveal_focused_delete(self, table_scroll=None):
        self.deliveries.append(table_scroll)
        super()._reveal_focused_delete(table_scroll)


def _pump_events(errors):
    # processEvents() raises a callback error instead of calling
    # sys.excepthook. Record it beside the hook's errors so one list
    # observes every channel a stale or failing reveal can report through.
    for _ in range(4):
        try:
            QApplication.processEvents()
        except Exception as error:
            errors.append((type(error), error, error.__traceback__))


def _run_exec_scenario(scenario):
    # exec() is the production loop; it reports callback errors only
    # through sys.excepthook, which the Sentry integration wraps. Entering it
    # here would also delete every deferred delete that earlier tests in this
    # process posted outside an event loop, and that crashes the full suite.
    # The child runs the same tasks_screen.py under its own QApplication.
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(TASKS_SCREEN.parents[1]), os.environ.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, str(CHILD), scenario], env=environment,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert Path(receipt["tasks_screen"]).resolve() == TASKS_SCREEN
    assert receipt["open_errors"] == []
    assert receipt["teardown_errors"] == []
    return receipt, result.stderr


def _center_inside(widget, viewport):
    return viewport.rect().contains(widget.mapTo(viewport, widget.rect().center()))


def _queue_reveal_then_destroy(page, screen, destroy_parent):
    screen._row_resize_timer.stop()
    screen._resize_task_rows()
    delete(page if destroy_parent else screen)
    assert not isValid(screen)


@pytest.fixture
def errors(monkeypatch):
    recorded = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: recorded.append(error))
    return recorded


@pytest.fixture
def open_board(errors):
    app = QApplication.instance() or QApplication([])
    pages = []

    def open_page(screen):
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setWidget(screen)
        screen.render(
            [{"id": f"task-{row}", "title": f"Task {row}"} for row in range(24)],
            [], mutations_enabled=True,
        )
        page.resize(420, 260)
        page.show()
        page.activateWindow()
        pages.append(page)
        _pump_events(errors)
        assert errors == []
        return page

    yield app, open_page
    for page in pages:
        if isValid(page):
            delete(page)
    _pump_events(errors)
    assert errors == []


@pytest.fixture
def board(open_board, errors):
    app, open_page = open_board
    deliveries = []
    screen = ObservedTasksScreen(deliveries)
    page = open_page(screen)
    deliveries.clear()
    return app, page, screen, deliveries, errors


@pytest.fixture
def plain_board(open_board, errors):
    # The production screen with no override: tests on this board observe
    # only outcomes (errors, focus, geometry), never which method Qt ran.
    app, open_page = open_board
    screen = TasksScreen(Strings("en"))
    return app, open_page(screen), screen, errors


@pytest.mark.parametrize("destroy_parent", [False, True], ids=["screen", "parent"])
def test_queued_reveal_is_cancelled_on_destruction(board, destroy_parent):
    _, page, screen, deliveries, errors = board
    screen._row_resize_timer.stop()
    screen._resize_task_rows()
    assert deliveries == []
    delete(page if destroy_parent else screen)
    assert not isValid(screen)
    _pump_events(errors)
    assert errors == []
    assert deliveries == []


@pytest.mark.parametrize("destroy_parent", [False, True], ids=["screen", "parent"])
def test_destroyed_screen_raises_nothing_from_event_loop(plain_board, destroy_parent):
    _, page, screen, errors = plain_board
    _queue_reveal_then_destroy(page, screen, destroy_parent)
    _pump_events(errors)
    assert errors == []


@pytest.mark.parametrize("destroy_parent", [False, True], ids=["screen", "parent"])
def test_destroyed_screen_reports_nothing_under_exec(destroy_parent):
    receipt, _ = _run_exec_scenario(
        "destroyed-parent" if destroy_parent else "destroyed-screen"
    )
    assert receipt["screen_valid"] is False
    assert receipt["reported"] == []


def test_surviving_queued_reveal_keeps_scroll_state_and_reveals_focus(board):
    _, page, screen, deliveries, errors = board
    table = screen.task_table
    button = table.cellWidget(23, 3)
    button.setFocus()
    _pump_events(errors)
    assert QApplication.focusWidget() is button
    page.verticalScrollBar().setValue(0)
    assert not _center_inside(button, page.viewport())
    screen._row_resize_timer.stop()
    deliveries.clear()
    screen._resize_task_rows()
    expected_scroll = (
        table.verticalScrollBar().value(), table.horizontalScrollBar().value()
    )
    assert deliveries == []
    _pump_events(errors)
    assert deliveries[0] == expected_scroll
    assert QApplication.focusWidget() is button
    assert _center_inside(button, page.viewport())
    assert errors == []


def test_surviving_reveal_recenters_table_then_page(plain_board):
    _, page, screen, errors = plain_board
    table = screen.task_table
    button = table.cellWidget(23, 3)
    button.setFocus()
    _pump_events(errors)
    assert QApplication.focusWidget() is button
    table.verticalScrollBar().setValue(0)
    page.verticalScrollBar().setValue(0)
    assert not _center_inside(button, table.viewport())
    screen._row_resize_timer.stop()
    screen._resize_task_rows()
    # The reveal is deferred, so nothing has recentered yet.
    assert not _center_inside(button, table.viewport())
    _pump_events(errors)
    assert QApplication.focusWidget() is button
    assert _center_inside(button, table.viewport())
    assert _center_inside(button, page.viewport())
    assert errors == []


def test_resize_timeout_cancels_reveal_after_deferred_parent_deletion(board):
    _, page, screen, deliveries, errors = board
    table = screen.task_table
    timeouts = []
    screen._row_resize_timer.stop()

    def delete_parent_after_resize():
        # Production resize runs first and must queue, not deliver, its reveal.
        timeouts.append(True)
        assert deliveries == []
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    screen._row_resize_timer.timeout.connect(delete_parent_after_resize)
    screen._schedule_row_resize()
    assert timeouts == []
    _pump_events(errors)
    assert timeouts == [True]
    assert not isValid(page)
    assert not isValid(screen)
    assert not isValid(table)
    assert errors == []
    assert deliveries == []


@pytest.mark.parametrize(
    "scenario,failure",
    [
        ("live-runtime-error", RuntimeError("live reveal failed")),
        ("live-attribute-error", AttributeError("live reveal failed")),
    ],
    ids=["runtime", "attribute"],
)
def test_live_reveal_error_reaches_excepthook_under_exec(scenario, failure):
    receipt, _ = _run_exec_scenario(scenario)
    assert receipt["lookups"] == 1
    assert receipt["reported"] == [repr(failure)]
    assert receipt["reported_expected_objects"] is True


def test_live_reveal_error_reports_once_inside_modal_dialog():
    # Account-resolution prompts run QDialog.exec() inside app.exec(). A reveal
    # error that escaped its slot there would leave the Python error set, so
    # the next Python override fails and Sentry gets a spurious SystemError.
    receipt, stderr = _run_exec_scenario("modal-dialog-errors")
    assert len(receipt["raised"]) == 3
    assert receipt["reported"] == receipt["raised"]
    assert receipt["reported_expected_objects"] is True
    assert "Error calling Python override" not in stderr
