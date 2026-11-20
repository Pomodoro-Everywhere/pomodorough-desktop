"""Every failure the window shows reaches Sentry, except plain offline.

The sentence the user reads is chosen by error type, so "That change was not
accepted" can come from many raise sites. Only the Sentry stack trace tells
them apart, so a shown failure that is never captured cannot be debugged.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough.cloud_request_errors import ServiceUnreachableError
from pomodorough.controller_outcomes import EmitFailure, EmitTaskFailure
from pomodorough.shared_core import SharedCoreOperationError
from pomodorough.shown_failure_report import report_shown_failure
from test_r2_a2_task_failure_notice import _dialogs, _window

CAPTURE = "pomodorough.shown_failure_report.capture_exception"

REPORTED = [
    ValueError("Timer changed before restart could be saved."),
    KeyError("phase"),
    TypeError("bad"),
    SharedCoreOperationError("rejected", "detail"),
    sqlite3.OperationalError("disk I/O error"),
    OSError("read-only file system"),
]
OFFLINE = [
    ServiceUnreachableError("offline"),
    urllib.error.URLError("no route"),
    ConnectionResetError("reset"),
    TimeoutError("timed out"),
]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize("error", REPORTED, ids=lambda error: type(error).__name__)
def test_defect_failure_is_captured(error):
    with patch(CAPTURE) as capture:
        report_shown_failure(error)
    capture.assert_called_once_with(error)


@pytest.mark.parametrize("error", OFFLINE, ids=lambda error: type(error).__name__)
def test_offline_failure_is_not_captured(error):
    with patch(CAPTURE) as capture:
        report_shown_failure(error)
    capture.assert_not_called()


def test_window_failure_is_captured_when_shown(app, tmp_path):
    error = ValueError("Timer changed before restart could be saved.")
    with _window(app, tmp_path) as window, _dialogs("en") as dialogs:
        with patch(CAPTURE) as capture:
            window._show_failure(EmitFailure(error))
    capture.assert_called_once_with(error)
    assert dialogs.messages() == [window.strings.text("failure.rejected")]


def test_window_task_failure_is_captured_when_shown(app, tmp_path):
    error = ValueError("Task title is empty.")
    with _window(app, tmp_path) as window, _dialogs("en"):
        failure = EmitTaskFailure("add", error, lambda: None)
        with patch(CAPTURE) as capture:
            window._show_task_failure(failure)
    capture.assert_called_once_with(error)
