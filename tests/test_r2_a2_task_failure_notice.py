"""UX round 2 A2 (DSK-17): task failures show one localized sentence.

The user never sees the exception text. Validation failures name what to
change; storage and Core failures offer Retry.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.shared_core import SharedCoreLoadError, SharedCoreOperationError
from test_r52_d06_task_header_wrap import _process, _tasks_window

LOCALES = ["en", "ar-XB"]
RAW_DETAIL = "disk I/O error: /Users/someone/state.sqlite3 (code 1802)"
INTERNAL_WORDS = ("sqlite", "Error", "Exception", "Traceback", "{", "bytes", "512")
TASK_CHANGES = ["add", "delete", "rename"]
STORE_WRITES = ("queue_task_operation", "queue_task_operation_with_selection")
CAPTURE = "pomodorough.timer_interaction_controller.capture_exception"


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


class _Dialogs:
    """Records what a modal shows and answers it without a nested loop."""

    def __init__(self, strings: Strings, answers: tuple[str, ...] = ()) -> None:
        self._strings = strings
        self._answers = list(answers)
        self.shown: list[tuple[str, list[str]]] = []

    def warning(self, _parent, _title, message, *_rest):
        self.shown.append((message, []))
        return QMessageBox.StandardButton.Ok

    def exec(self, dialog: QMessageBox) -> int:
        buttons = dialog.buttons()
        if len(buttons) == 1:
            # A plain notice: one localized OK button and nothing to choose.
            self.shown.append((dialog.text(), []))
            return 0
        self.shown.append((dialog.text(), [button.text() for button in buttons]))
        answer = self._strings.text(self._answers.pop(0))
        next(button for button in buttons if button.text() == answer).click()
        return 0

    def messages(self) -> list[str]:
        return [message for message, _buttons in self.shown]


@contextmanager
def _dialogs(locale: str, *answers: str):
    dialogs = _Dialogs(Strings(locale), answers)
    with (
        patch.object(QMessageBox, "warning", dialogs.warning),
        patch.object(QMessageBox, "exec", lambda dialog: dialogs.exec(dialog)),
    ):
        yield dialogs


@contextmanager
def _window(app, tmp_path, locale: str = "en"):
    with _tasks_window(app, tmp_path, locale, 640, 360) as window:
        yield window


@contextmanager
def _failing_store(window, error: Exception, failures: int | None = None):
    """Fail task writes ``failures`` times (always when None), then write."""
    attempts: list[str] = []

    def fail_then_write(name, real):
        def write(*args, **kwargs):
            attempts.append(name)
            if failures is None or len(attempts) <= failures:
                raise error
            return real(*args, **kwargs)

        return write

    store = window.store
    with (
        patch.object(
            store, STORE_WRITES[0], fail_then_write(STORE_WRITES[0], store.queue_task_operation)
        ),
        patch.object(
            store,
            STORE_WRITES[1],
            fail_then_write(STORE_WRITES[1], store.queue_task_operation_with_selection),
        ),
    ):
        yield attempts


def _titles(window) -> list[str]:
    return sorted(task["title"] for task in window.tasks)


def _seed(app, window) -> str:
    window._add_task("Seeded task")
    _process(app, 10)
    return next(task["id"] for task in window.tasks if task["title"] == "Seeded task")


def _request(window, change: str, task_id: str) -> None:
    if change == "add":
        window._add_task("Second task")
    elif change == "delete":
        window._delete_task(task_id)
    else:
        window._rename_task(task_id, "Renamed task")


def _is_localized(message: str, locale: str) -> bool:
    return locale == "en" or (message.startswith("⟦‼ ") and message.endswith(" ‼⟧"))


def _assert_no_internal_text(message: str) -> None:
    assert RAW_DETAIL not in message
    assert [word for word in INTERNAL_WORDS if word in message] == []


# --- Names the title rules reject -------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("title", "key"),
    [("\t\n", "task.error.blank_title"), ("x" * 600, "task.error.title_too_long")],
)
def test_rejected_new_task_name_is_one_localized_sentence(app, tmp_path, locale, title, key):
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        window._add_task(title)
        assert len(dialogs.shown) == 1
        message, buttons = dialogs.shown[0]
        _assert_no_internal_text(message)
        assert _is_localized(message, locale), message
        assert message == Strings(locale).text(key)
        # The user fixes the name; a retry with the same name fails again.
        assert buttons == []
        assert window.tasks == []


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    ("title", "key"),
    [("", "task.error.blank_title"), ("é" * 300, "task.error.title_too_long")],
)
def test_rejected_rename_is_one_localized_sentence(app, tmp_path, locale, title, key):
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        task_id = _seed(app, window)
        window._rename_task(task_id, title)
        message = dialogs.messages()[0]
        _assert_no_internal_text(message)
        assert _is_localized(message, locale), message
        assert dialogs.shown == [(Strings(locale).text(key), [])]
        assert _titles(window) == ["Seeded task"]


def test_title_rules_keep_their_contract_for_other_callers():
    # The command line and storage still catch ValueError and print the text.
    with pytest.raises(ValueError, match="at least one printable character"):
        task_from_title("\t")
    with pytest.raises(ValueError, match="512 bytes or fewer"):
        task_from_title("x" * 513)
    assert task_from_title("x" * 512)["title"] == "x" * 512


# --- Storage and Core failures ----------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("change", TASK_CHANGES)
def test_storage_failure_shows_one_sentence_with_retry(app, tmp_path, locale, change):
    strings = Strings(locale)
    error = sqlite3.OperationalError(RAW_DETAIL)
    with _window(app, tmp_path, locale) as window:
        task_id = _seed(app, window)
        with (
            _dialogs(locale, "task.error.cancel") as dialogs,
            _failing_store(window, error) as attempts,
            patch(CAPTURE) as capture,
        ):
            _request(window, change, task_id)
        assert len(dialogs.shown) == 1
        message, buttons = dialogs.shown[0]
        _assert_no_internal_text(message)
        assert buttons == [strings.text("task.error.retry"), strings.text("task.error.cancel")]
        assert message == strings.text(f"task.error.{change}_failed")
        # The raw detail still reaches the existing diagnostic path, once.
        capture.assert_called_once_with(error)
        assert len(attempts) == 1
        assert _titles(window) == ["Seeded task"]


@pytest.mark.parametrize("change", TASK_CHANGES)
def test_retry_runs_the_task_change_again(app, tmp_path, change):
    expected = {
        "add": ["Second task", "Seeded task"],
        "delete": [],
        "rename": ["Renamed task"],
    }
    with _window(app, tmp_path) as window:
        task_id = _seed(app, window)
        with (
            _dialogs("en", "task.error.retry") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL), failures=1),
            patch(CAPTURE),
        ):
            _request(window, change, task_id)
            _process(app, 10)
        assert [buttons for _message, buttons in dialogs.shown] == [["Retry", "Cancel"]]
        assert _titles(window) == expected[change]


def test_retry_that_fails_again_asks_again(app, tmp_path):
    with _window(app, tmp_path) as window:
        with (
            _dialogs("en", "task.error.retry", "task.error.cancel") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL)) as attempts,
            patch(CAPTURE) as capture,
        ):
            window._add_task("Never saved")
        sentence = Strings("en").text("task.error.add_failed")
        assert dialogs.shown == [(sentence, ["Retry", "Cancel"])] * 2
        assert len(attempts) == 2
        assert capture.call_count == 2
        assert window.tasks == []


def test_core_failure_offers_retry(app, tmp_path):
    with _window(app, tmp_path) as window:
        with (
            _dialogs("en", "task.error.cancel") as dialogs,
            _failing_store(window, SharedCoreLoadError(RAW_DETAIL)),
            patch(CAPTURE),
        ):
            window._add_task("Core rejects")
        assert dialogs.shown == [
            (Strings("en").text("task.error.add_failed"), ["Retry", "Cancel"])
        ]


@pytest.mark.parametrize("locale", LOCALES)
def test_unclassified_rejection_shows_a_sentence_without_the_detail(app, tmp_path, locale):
    rejection = ValueError('{"code": "task_id_mismatch", "detail": "' + RAW_DETAIL + '"}')
    with _window(app, tmp_path, locale) as window:
        with (
            _dialogs(locale) as dialogs,
            _failing_store(window, rejection),
            patch(CAPTURE) as capture,
        ):
            window._add_task("Rejected task")
        message = dialogs.messages()[0]
        _assert_no_internal_text(message)
        assert dialogs.shown == [(Strings(locale).text("task.error.change_rejected"), [])]
        # A rejected value is not an infrastructure failure: no new report.
        capture.assert_not_called()


def test_failed_dismissed_delete_brings_the_task_back(app, tmp_path):
    with _window(app, tmp_path) as window:
        _seed(app, window)
        table = window.task_table
        table.cellWidget(0, 3).click()
        _process(app, 10)
        assert table.rowCount() == 0
        with (
            _dialogs("en", "task.error.cancel") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL)),
            patch(CAPTURE),
        ):
            window.tasks_screen.undo_banner.dismiss_button.click()
            _process(app, 10)
        assert dialogs.shown == [
            (Strings("en").text("task.error.delete_failed"), ["Retry", "Cancel"])
        ]
        assert window.tasks_screen.pending_delete_task_id() is None
        assert table.rowCount() == 1
        assert _titles(window) == ["Seeded task"]


def test_retry_dialog_is_not_stacked_on_an_open_notice(app, tmp_path):
    # R50-D02: at most one warning is active at a time.
    with _window(app, tmp_path) as window:
        with (
            _dialogs("en") as dialogs,
            _failing_store(window, OSError(RAW_DETAIL)) as attempts,
            patch(CAPTURE),
        ):
            window._notice_dialog_active = True
            try:
                window._add_task("Behind a notice")
            finally:
                window._notice_dialog_active = False
        assert dialogs.shown == []
        assert len(attempts) == 1


# --- Wording and dialog shape -----------------------------------------------


@pytest.mark.parametrize(
    ("error", "key", "can_retry"),
    [
        (PermissionError(RAW_DETAIL), "task.error.delete_failed", True),
        (sqlite3.IntegrityError(RAW_DETAIL), "task.error.delete_failed", True),
        (SharedCoreLoadError(RAW_DETAIL), "task.error.delete_failed", True),
        # The Core rejected the value; the same request fails again. L9: a
        # rejected delete has its own sentence, with no task name to check.
        (
            SharedCoreOperationError("task_upsert", RAW_DETAIL),
            "task.error.delete_rejected",
            False,
        ),
        (ValueError(RAW_DETAIL), "task.error.delete_rejected", False),
        (KeyError(RAW_DETAIL), "task.error.delete_rejected", False),
    ],
)
@pytest.mark.parametrize("locale", LOCALES)
def test_failures_are_classified_by_type(locale, error, key, can_retry):
    from pomodorough.controller_outcomes import TaskChange
    from pomodorough.task_failure_notice import describe_task_failure

    strings = Strings(locale)
    notice = describe_task_failure(strings, TaskChange.DELETE, error)
    assert (notice.sentence, notice.can_retry) == (strings.text(key), can_retry)
    assert RAW_DETAIL not in notice.sentence


@pytest.mark.parametrize("change", ["ADD", "RENAME"])
@pytest.mark.parametrize(
    "error", [SharedCoreOperationError("task_upsert", RAW_DETAIL), ValueError(RAW_DETAIL)]
)
def test_rejected_add_and_rename_keep_the_change_rejected_sentence(change, error):
    from pomodorough.controller_outcomes import TaskChange
    from pomodorough.task_failure_notice import describe_task_failure

    strings = Strings("en")
    notice = describe_task_failure(strings, TaskChange[change], error)
    assert notice == type(notice)(strings.text("task.error.change_rejected"), False)


def test_failure_sentences_are_single_sentences_without_internal_terms():
    strings = Strings("en")
    keys = [
        key
        for key in strings.messages
        if key.startswith("task.error.") and key not in ("task.error.retry", "task.error.cancel")
    ]
    # B2 added delete_rejected (L9) and delete_unfinished (M2).
    assert len(keys) == 8
    for key in keys:
        sentence = strings.text(key)
        _assert_no_internal_text(sentence)
        assert sentence.endswith("."), key
        assert sentence.count(".") == 1, key


def test_retry_dialog_defaults_to_retry_and_escapes_to_cancel(app):
    from pomodorough.task_failure_notice import build_retry_dialog

    parent = QWidget()
    strings = Strings("ar-XB")
    dialog, retry_button = build_retry_dialog(parent, strings, "Sentence.")
    assert dialog.defaultButton() is retry_button
    assert retry_button.text() == strings.text("task.error.retry")
    assert dialog.escapeButton().text() == strings.text("task.error.cancel")
    assert dialog.text() == "Sentence."
    parent.deleteLater()
