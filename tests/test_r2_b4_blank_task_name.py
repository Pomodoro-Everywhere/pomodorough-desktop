"""UX round 2 B4 (DSK-07): the task composer rejects a blank draft.

A draft that is empty or only whitespace gets the localized blank-name
sentence and creates nothing. A draft with other characters is passed on
unchanged: edge spaces are part of the title, a pinned Core rule. The
title rules that the command line, the terminal view and storage use stay
as they were, so an all-space task that already exists is still valid.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from pomodorough.core import (
    BlankTaskTitleError,
    normalize_task_title,
    task_from_title,
)
from pomodorough.localization import Strings
from test_r52_d06_task_header_wrap import _process, _tasks_window

LOCALES = ["en", "ar-XB"]
# Each draft is blank after the title rules trim non-printable edges; the
# first four are accepted as titles by the unchanged title rules.
BLANK_DRAFTS = [" ", "   ", "   ", " \n ", "", "\t", " "]
STORE_WRITES = ("queue_task_operation", "queue_task_operation_with_selection")


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


class _Dialogs:
    """Records each modal sentence and its buttons without a nested loop.

    The notice modal is recorded whether the window shows it through the
    static warning call or through a dialog with its own buttons.
    """

    def __init__(self, strings: Strings) -> None:
        self._retry_label = strings.text("task.error.retry")
        self.shown: list[tuple[str, list[str]]] = []

    def warning(self, _parent, _title, message, *_rest):
        self.shown.append((message, []))
        return QMessageBox.StandardButton.Ok

    def exec(self, dialog: QMessageBox) -> int:
        labels = [button.text() for button in dialog.buttons()]
        self.shown.append((dialog.text(), labels))
        return 0

    def messages(self) -> list[str]:
        return [message for message, _labels in self.shown]

    def offered_retry(self) -> bool:
        return any(self._retry_label in labels for _message, labels in self.shown)


@contextmanager
def _dialogs(locale: str):
    dialogs = _Dialogs(Strings(locale))
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
def _store_writes(window):
    """Record every task write the window's store receives."""
    writes: list[str] = []

    def recording(name, real):
        def write(*args, **kwargs):
            writes.append(name)
            return real(*args, **kwargs)

        return write

    patches = [
        patch.object(window.store, name, recording(name, getattr(window.store, name)))
        for name in STORE_WRITES
    ]
    for item in patches:
        item.start()
    try:
        yield writes
    finally:
        for item in patches:
            item.stop()


def _titles(window) -> list[str]:
    return sorted(task["title"] for task in window.tasks)


def _row_titles(window) -> list[str]:
    table = window.tasks_screen.task_table
    return [table.item(row, 0).text() for row in range(table.rowCount())]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("draft", BLANK_DRAFTS)
def test_blank_draft_gets_the_blank_name_sentence(app, tmp_path, locale, draft):
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        with _store_writes(window) as writes:
            window._add_task(draft)
            _process(app)
        assert dialogs.messages() == [Strings(locale).text("task.error.blank_title")]
        assert not dialogs.offered_retry()
        assert writes == []
        assert window.tasks == []
        assert _row_titles(window) == []


@pytest.mark.parametrize("key", ["return", "button"])
def test_composer_rejects_typed_spaces(app, tmp_path, key):
    with _window(app, tmp_path) as window, _dialogs("en") as dialogs:
        screen = window.tasks_screen
        screen.task_input.setFocus()
        QTest.keyClicks(screen.task_input, "   ")
        if key == "return":
            QTest.keyClick(screen.task_input, Qt.Key.Key_Return)
        else:
            QTest.mouseClick(screen.add_task_button, Qt.MouseButton.LeftButton)
        _process(app)
        assert dialogs.messages() == [Strings("en").text("task.error.blank_title")]
        assert _row_titles(window) == []
        # The composer stays usable for the corrected name.
        assert screen.task_input.isEnabled()
        screen.task_input.clear()
        QTest.keyClicks(screen.task_input, "Write report")
        QTest.keyClick(screen.task_input, Qt.Key.Key_Return)
        _process(app)
        assert _row_titles(window) == ["Write report"]
        assert screen.task_input.text() == ""


def test_blank_draft_keeps_the_selected_task(app, tmp_path):
    with _window(app, tmp_path) as window, _dialogs("en"):
        window._add_task("Selected task")
        _process(app)
        selected = window.settings["selectedTaskId"]
        window._add_task("   ")
        _process(app)
        assert window.settings["selectedTaskId"] == selected
        assert window.store.load()["settings"]["selectedTaskId"] == selected
        assert _titles(window) == ["Selected task"]


@pytest.mark.parametrize(
    "draft", ["  Write report ", " a", "a ", "a   b", "  task  ", " . "]
)
def test_draft_with_other_characters_is_not_trimmed(app, tmp_path, draft):
    with _window(app, tmp_path) as window, _dialogs("en") as dialogs:
        window._add_task(draft)
        _process(app)
        assert dialogs.shown == []
        expected = task_from_title(draft)
        assert window.tasks == [expected]
        assert expected["title"] == normalize_task_title(draft)
        assert _row_titles(window) == [expected["title"]]


def test_title_rules_for_other_callers_are_unchanged():
    # Storage, replication, the command line and the terminal view share
    # these rules; an all-space title stays valid there.
    assert normalize_task_title("   ") == "   "
    assert task_from_title("   ")["title"] == "   "
    assert task_from_title(" task ")["title"] == " task "
    with pytest.raises(BlankTaskTitleError):
        task_from_title("\t ")


def test_existing_all_space_task_is_still_listed(app, tmp_path):
    with _window(app, tmp_path) as window, _dialogs("en") as dialogs:
        legacy = task_from_title("   ")
        window.store.queue_task_operation("upsert", legacy)
        window._load_state()
        window._render()
        _process(app)
        assert dialogs.shown == []
        assert window.tasks == [legacy]
        assert _row_titles(window) == ["   "]
        # The same name again is a blank draft, not a duplicate selection.
        window._add_task("   ")
        assert dialogs.messages() == [Strings("en").text("task.error.blank_title")]


@pytest.mark.parametrize(
    ("draft", "blank"),
    [(draft, True) for draft in BLANK_DRAFTS]
    + [(" a ", False), ("a", False), (" . ", False), ("​", True), ("_", False)],
)
def test_blank_draft_definition(draft, blank):
    from pomodorough.task_draft_policy import is_blank_task_draft

    assert is_blank_task_draft(draft) is blank
