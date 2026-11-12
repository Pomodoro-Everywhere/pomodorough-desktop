"""UX round 2 B4: renaming a task to a blank title gets the blank-name sentence.

Rename follows the composer rule. A title that is empty or only whitespace
after the title rules is refused with the localized blank-name sentence,
without a retry action, and the task keeps its old name. Any other title is
kept exactly as typed, edge spaces included.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication
from test_r2_b4_blank_task_name import (
    BLANK_DRAFTS,
    _dialogs,
    _row_titles,
    _store_writes,
    _window,
)
from test_r52_d06_task_header_wrap import _process
from test_ux_rename_task import _commit, _press_f2

from pomodorough.controller_outcomes import EmitTaskFailure
from pomodorough.core import BlankTaskTitleError, task_from_title
from pomodorough.localization import Strings

LOCALES = ["en", "ar-XB"]


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _seed(app, window, title: str) -> dict[str, str]:
    window._add_task(title)
    _process(app)
    return task_from_title(title)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("typed", ["", "   "])
def test_typed_blank_rename_gets_the_blank_name_sentence(app, tmp_path, locale, typed):
    with _window(app, tmp_path, locale) as window, _dialogs(locale) as dialogs:
        task = _seed(app, window, "Write report")
        with _store_writes(window) as writes:
            editor = _press_f2(window.tasks_screen, 0)
            _commit(editor, typed)
            _process(app)
        assert dialogs.messages() == [Strings(locale).text("task.error.blank_title")]
        assert not dialogs.offered_retry()
        assert writes == []
        assert window.tasks == [task]
        assert _row_titles(window) == ["Write report"]


@pytest.mark.parametrize("draft", BLANK_DRAFTS)
def test_controller_refuses_every_blank_rename(app, tmp_path, draft):
    with _window(app, tmp_path) as window, _dialogs("en"):
        task = _seed(app, window, "Write report")
        before = window.store.load()["pendingTasks"]
        outcome = window._owned_controller("timer").rename_task(task["id"], draft)
        assert [type(effect) for effect in outcome.effects] == [EmitTaskFailure]
        assert isinstance(outcome.effects[0].error, BlankTaskTitleError)
        assert window.store.load()["pendingTasks"] == before
        assert window.settings["selectedTaskId"] == task["id"]


@pytest.mark.parametrize("typed", ["  Plan week ", " a", "a ", " . "])
def test_rename_keeps_other_titles_as_typed(app, tmp_path, typed):
    with _window(app, tmp_path) as window, _dialogs("en") as dialogs:
        _seed(app, window, "Write report")
        editor = _press_f2(window.tasks_screen, 0)
        _commit(editor, typed)
        _process(app)
        assert dialogs.shown == []
        expected = task_from_title(typed)
        assert expected["title"] == typed
        assert window.tasks == [expected]
        assert _row_titles(window) == [typed]
