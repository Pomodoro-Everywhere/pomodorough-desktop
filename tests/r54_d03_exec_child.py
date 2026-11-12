"""Standalone R54-D03 exec() scenarios, each in its own QApplication.

app.exec() is the production loop, and it reports slot errors only through
sys.excepthook. Entering it also deletes every deferred delete that was posted
outside an event loop. In a long pytest process that includes stale widgets
from earlier tests, and deleting them crashes the interpreter. Each scenario
therefore runs in a fresh interpreter and prints one JSON receipt.
"""

from __future__ import annotations

import json
import os
import sys
from functools import partial

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea
from shiboken6 import delete, isValid

from pomodorough import tasks_screen
from pomodorough.localization import Strings


def pump_events(errors):
    # processEvents() raises a slot error instead of calling sys.excepthook.
    for _ in range(4):
        try:
            QApplication.processEvents()
        except Exception as error:
            errors.append(error)


def open_board(errors):
    screen = tasks_screen.TasksScreen(Strings("en"))
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
    pump_events(errors)
    return page, screen


def destroyed_screen(app, page, screen, destroy_parent):
    screen._row_resize_timer.stop()
    screen._resize_task_rows()
    delete(page if destroy_parent else screen)
    observed = {"screen_valid": isValid(screen)}
    QTimer.singleShot(50, app.quit)
    app.exec()
    return observed, []


def live_reveal_error(app, page, screen, failure):
    lookups = []

    def fail_focus_lookup():
        lookups.append(True)
        # Posted behind the running reveal, so exec() finishes delivering it.
        QTimer.singleShot(0, app, app.quit)
        raise failure

    screen._focused_delete_id = fail_focus_lookup
    screen._row_resize_timer.stop()
    QTimer.singleShot(5000, page, app.quit)  # Bound; cancelled with the page.
    screen._resize_task_rows()
    app.exec()
    return {"lookups": len(lookups)}, [failure]


def reveal_errors_inside_modal_dialog(app, page, screen):
    raised = []

    def fail_focus_lookup():
        raised.append(RuntimeError(f"reveal failed in dialog {len(raised)}"))
        raise raised[-1]

    def run_dialogs():
        # A single pass did not leak in every probe run, so run three.
        for _ in range(3):
            screen._row_resize_timer.stop()
            screen._resize_task_rows()
            dialog = QDialog(page)
            QTimer.singleShot(50, dialog, dialog.reject)
            dialog.exec()
        app.quit()

    screen._focused_delete_id = fail_focus_lookup
    QTimer.singleShot(10000, page, app.quit)  # Bound; cancelled with the page.
    QTimer.singleShot(0, page, run_dialogs)
    app.exec()
    return {"raised": [repr(error) for error in raised]}, raised


SCENARIOS = {
    "destroyed-screen": partial(destroyed_screen, destroy_parent=False),
    "destroyed-parent": partial(destroyed_screen, destroy_parent=True),
    "live-runtime-error": partial(
        live_reveal_error, failure=RuntimeError("live reveal failed")
    ),
    "live-attribute-error": partial(
        live_reveal_error, failure=AttributeError("live reveal failed")
    ),
    "modal-dialog-errors": reveal_errors_inside_modal_dialog,
}


def run(scenario):
    errors = []
    sys.excepthook = lambda kind, error, trace: errors.append(error)
    app = QApplication([])
    page, screen = open_board(errors)
    receipt = {
        "tasks_screen": tasks_screen.__file__,
        "open_errors": [repr(error) for error in errors],
    }
    errors.clear()
    observed, expected = SCENARIOS[scenario](app, page, screen)
    reported = list(errors)
    errors.clear()
    if isValid(page):
        delete(page)
    pump_events(errors)
    receipt.update(
        observed,
        reported=[repr(error) for error in reported],
        # Exceptions compare by identity, so this proves the reported errors
        # are the raised objects, not copies or follow-on errors.
        reported_expected_objects=reported == expected,
        teardown_errors=[repr(error) for error in errors],
    )
    return receipt


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1])))
