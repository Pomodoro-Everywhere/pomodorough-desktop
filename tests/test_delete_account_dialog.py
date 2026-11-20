"""DSK-10: the delete-account confirmation wraps and explains a mismatch.

The old QInputDialog put the whole prompt on one line, about 1,260 px
wide, and a wrong word closed the dialog with only a 10 s status message.

The mismatch notice must not take its room from the prompt: the dialog
grows, within the screen, until every wrapped sentence is whole.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel
from test_r52_d06_task_header_wrap import _process, _scaled_font, _tasks_window

from pomodorough.delete_account_dialog import (
    CONFIRMATION_WORD,
    DeleteAccountDialog,
    ask_delete_account_confirmation,
)
from pomodorough.localization import Strings


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture(params=["en", "ar-XB"])
def dialog(request, app) -> Iterator[DeleteAccountDialog]:
    view = DeleteAccountDialog(None, Strings(request.param))
    view.show()
    app.processEvents()
    yield view
    view.done(QDialog.DialogCode.Rejected)
    view.deleteLater()
    app.processEvents()


def test_dialog_wraps_the_prompt_within_the_screen(dialog, app) -> None:
    screen = app.primaryScreen().availableGeometry()

    assert dialog.prompt.wordWrap()
    assert dialog.frameGeometry().width() <= screen.width()
    # Wrapped: the prompt takes several lines instead of one long line.
    line = dialog.prompt.fontMetrics().lineSpacing()
    assert dialog.prompt.height() >= 2 * line


@pytest.mark.parametrize("typed", ["delete", "DELETE ", "DELET", ""])
def test_delete_button_stays_disabled_until_the_exact_word(dialog, typed) -> None:
    QTest.keyClicks(dialog.confirmation_input, typed)

    assert not dialog.delete_button.isEnabled()


def test_exact_word_enables_delete_and_enter_accepts(dialog) -> None:
    QTest.keyClicks(dialog.confirmation_input, CONFIRMATION_WORD)
    assert dialog.delete_button.isEnabled()

    QTest.keyClick(dialog.confirmation_input, Qt.Key.Key_Return)

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.confirmation() == CONFIRMATION_WORD


def test_enter_with_a_wrong_word_keeps_the_dialog_open_and_explains(dialog) -> None:
    QTest.keyClicks(dialog.confirmation_input, "delete")

    QTest.keyClick(dialog.confirmation_input, Qt.Key.Key_Return)

    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.mismatch_notice.isVisible()
    assert dialog.confirmation_input.accessibleDescription() == (
        dialog.mismatch_notice.text()
    )

    QTest.keyClicks(dialog.confirmation_input, "x")

    assert not dialog.mismatch_notice.isVisible()


def test_cancel_is_named_and_rejects(dialog) -> None:
    cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
    assert cancel is not None
    assert not cancel.isDefault()

    cancel.click()

    assert dialog.result() == QDialog.DialogCode.Rejected


def test_delete_button_is_named_for_its_action(dialog) -> None:
    role = dialog.buttons.buttonRole(dialog.delete_button)

    assert role == QDialogButtonBox.ButtonRole.DestructiveRole
    assert dialog.delete_button.text() == dialog.strings.text("account.delete_confirm")


LOCALES = pytest.mark.parametrize("locale", ["en", "ar-XB"])
FONT_SCALES = pytest.mark.parametrize("scale", [1, 1.5, 2])


@contextmanager
def _delete_dialog(
    app: QApplication, tmp_path: Path, locale: str, scale: float
) -> Iterator[DeleteAccountDialog]:
    """The dialog over the default main window, so the app stylesheet applies."""
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * scale),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        view = DeleteAccountDialog(window, window.strings)
        view.show()
        _process(app)
        try:
            yield view
        finally:
            view.done(QDialog.DialogCode.Rejected)
            view.deleteLater()
            _process(app)


def _cut_text(label: QLabel) -> list[str]:
    """Why a wrapped label does not show its whole text; empty when it does."""
    problems = []
    needed = label.heightForWidth(label.width())
    if label.height() < needed:
        problems.append(f"height {label.height()} of {needed}")
    room = label.contentsRect()
    wrapped = label.fontMetrics().boundingRect(
        QRect(0, 0, room.width(), 0), int(Qt.TextFlag.TextWordWrap), label.text()
    )
    if wrapped.height() > room.height() or wrapped.width() > room.width():
        problems.append(f"text {wrapped.size().toTuple()} in {room.size().toTuple()}")
    if label.visibleRegion().boundingRect() != label.rect():
        problems.append("partly outside the dialog")
    return problems


def _enter_a_wrong_word(app: QApplication, view: DeleteAccountDialog) -> None:
    QTest.keyClicks(view.confirmation_input, "delete")
    QTest.keyClick(view.confirmation_input, Qt.Key.Key_Return)
    _process(app)


def _inside_the_screen(view: DeleteAccountDialog) -> bool:
    return view.screen().availableGeometry().contains(view.frameGeometry())


@LOCALES
@FONT_SCALES
def test_prompt_is_whole_when_the_dialog_opens(app, tmp_path, locale, scale) -> None:
    with _delete_dialog(app, tmp_path, locale, scale) as view:
        assert _cut_text(view.prompt) == []
        assert _inside_the_screen(view)


@LOCALES
@FONT_SCALES
def test_prompt_stays_whole_when_the_mismatch_notice_shows(
    app, tmp_path, locale, scale
) -> None:
    with _delete_dialog(app, tmp_path, locale, scale) as view:
        prompt_height = view.prompt.height()

        _enter_a_wrong_word(app, view)

        assert view.mismatch_notice.isVisible()
        assert view.prompt.height() >= prompt_height
        assert _cut_text(view.prompt) == []
        assert _cut_text(view.mismatch_notice) == []
        assert _inside_the_screen(view)


@LOCALES
@FONT_SCALES
def test_notice_keeps_its_room_once_it_hides(app, tmp_path, locale, scale) -> None:
    """Typing again hides the notice; the prompt and the field stay in place."""
    with _delete_dialog(app, tmp_path, locale, scale) as view:
        _enter_a_wrong_word(app, view)
        shown = (view.size(), view.prompt.geometry(), view.confirmation_input.geometry())

        QTest.keyClicks(view.confirmation_input, "x")
        _process(app)

        assert not view.mismatch_notice.isVisible()
        hidden = (view.size(), view.prompt.geometry(), view.confirmation_input.geometry())
        assert hidden == shown
        assert _cut_text(view.prompt) == []


def _narrower_screen(app, tmp_path, locale, scale) -> QRect:
    """A screen area between the dialog's minimum width and its size hint.

    Measured on the real dialog, so the case does not depend on the font the
    platform gives the tests. The height is the height the dialog opens with.
    """
    with _delete_dialog(app, tmp_path, locale, scale) as view:
        frame = view.frameGeometry().size() - view.size()
        width = (view.sizeHint().width() + view.minimumWidth()) // 2
        assert view.layout().totalHeightForWidth(width) > view.height()
        return QRect(0, 0, width + frame.width(), view.frameGeometry().height())


@LOCALES
@FONT_SCALES
def test_dialog_on_a_narrower_screen_grows_taller_for_its_text(
    app, tmp_path, locale, scale
) -> None:
    area = _narrower_screen(app, tmp_path, locale, scale)
    area.setHeight(app.primaryScreen().availableGeometry().height())
    with (
        patch.object(DeleteAccountDialog, "_screen_area", return_value=area),
        _delete_dialog(app, tmp_path, locale, scale) as view,
    ):
        assert area.contains(view.frameGeometry())
        assert _cut_text(view.prompt) == []

        _enter_a_wrong_word(app, view)

        assert area.contains(view.frameGeometry())
        assert _cut_text(view.prompt) == []
        assert _cut_text(view.mismatch_notice) == []


@LOCALES
@FONT_SCALES
def test_dialog_never_grows_past_the_screen(app, tmp_path, locale, scale) -> None:
    """The text wants more than this screen has; the dialog stays inside it."""
    area = _narrower_screen(app, tmp_path, locale, scale)
    with (
        patch.object(DeleteAccountDialog, "_screen_area", return_value=area),
        _delete_dialog(app, tmp_path, locale, scale) as view,
    ):
        assert area.contains(view.frameGeometry())

        _enter_a_wrong_word(app, view)

        assert view.mismatch_notice.isVisible()
        assert area.contains(view.frameGeometry())


def test_dialog_the_user_enlarged_keeps_its_size(app, tmp_path) -> None:
    with _delete_dialog(app, tmp_path, "en", 1) as view:
        enlarged = view.size() + view.sizeHint()
        view.resize(enlarged)
        _process(app)

        _enter_a_wrong_word(app, view)

        assert view.size() == enlarged
        assert _cut_text(view.prompt) == []


def test_dialog_that_grows_at_the_screen_edge_moves_back_inside(app, tmp_path) -> None:
    with _delete_dialog(app, tmp_path, "en", 1) as view:
        corner = view.screen().availableGeometry().bottomRight()
        frame = view.frameGeometry()
        view.move(corner.x() - frame.width() + 1, corner.y() - frame.height() + 1)
        _process(app)
        assert view.frameGeometry().bottomRight() == corner
        assert _inside_the_screen(view)

        _enter_a_wrong_word(app, view)

        assert _cut_text(view.prompt) == []
        assert _inside_the_screen(view)


@LOCALES
def test_modal_dialog_from_the_window_shows_the_whole_prompt(
    app, tmp_path, locale
) -> None:
    """The real entry point: a modal dialog over the main window at 2x."""
    seen: dict[str, list[str]] = {}

    def wrong_word_then_cancel() -> None:
        view = app.activeModalWidget()
        seen["opened"] = _cut_text(view.prompt)
        _enter_a_wrong_word(app, view)
        seen["mismatch"] = _cut_text(view.prompt) + _cut_text(view.mismatch_notice)
        view.reject()

    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 2),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        QTimer.singleShot(0, wrong_word_then_cancel)
        assert ask_delete_account_confirmation(window, window.strings) is None

    assert seen == {"opened": [], "mismatch": []}
