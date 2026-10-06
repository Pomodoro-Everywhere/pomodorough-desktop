"""Exercise Qt shortcut arbitration and native key handling, not callbacks alone."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QVBoxLayout, QWidget
from shiboken6 import delete

from pomodorough.localization import Strings
from pomodorough.tasks_screen import TasksScreen
from pomodorough.timer_screen import TimerScreen
from pomodorough.ui_views import MainWindowViewMixin


class ShortcutWindow(MainWindowViewMixin, QWidget):
    def __init__(self):
        super().__init__()
        self.actions = []
        self.timer = TimerScreen(Strings("en"), {
            "durations": {"focus": 25, "short_break": 5, "long_break": 15},
            "autoStartBreaks": False,
        })
        self.tasks = TasksScreen(Strings("en"))
        self.tasks.render([{"id": "task-1", "title": "Task"}], [], mutations_enabled=True)
        self.editor = QPlainTextEdit()
        layout = QVBoxLayout(self)
        for widget in (self.timer, self.tasks, self.editor):
            layout.addWidget(widget)
        self.timer.set_settings_visible(True)
        self.timer.primary_action_requested.connect(self._primary_action)
        self.timer.command_requested.connect(self._issue)
        self.timer.auto_breaks_changed.connect(lambda value: self.actions.append(value))
        self.tasks.delete_task_requested.connect(self.actions.append)
        self._build_shortcuts()

    def _primary_action(self):
        self.actions.append("primary")

    def _issue(self, command):
        self.actions.append(command)


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    view = ShortcutWindow()
    view.show()
    view.activateWindow()
    app.processEvents()
    yield view
    view.close()
    delete(view)
    app.processEvents()


def focus(widget):
    widget.setFocus()
    QApplication.processEvents()
    assert QApplication.focusWidget() is widget


def space(widget):
    QTest.keyClick(widget, Qt.Key.Key_Space)
    QApplication.processEvents()


def repeat_space(widget):
    for kind in (QEvent.Type.KeyRelease, QEvent.Type.KeyPress):
        QApplication.sendEvent(widget, QKeyEvent(
            kind, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", True, 1,
        ))


@pytest.mark.parametrize("name,expected", [
    ("primary_button", "primary"), ("finish_button", "finish"),
    ("cancel_button", "cancel"), ("delete", "task-1"),
])
def test_native_buttons_activate_on_release_once(window, name, expected):
    button = (window.tasks.task_table.cellWidget(0, 3) if name == "delete"
              else getattr(window.timer, name))
    focus(button)
    QTest.keyPress(button, Qt.Key.Key_Space)
    assert button.isDown()
    assert window.actions == []
    repeat_space(button)
    assert button.isDown()
    assert window.actions == []
    QTest.keyRelease(button, Qt.Key.Key_Space)
    assert window.actions == [expected]
    assert not button.isDown()


def test_checkbox_toggles_in_both_directions(window):
    checkbox = window.timer.auto_breaks
    focus(checkbox)
    space(checkbox)
    assert checkbox.isChecked()
    space(checkbox)
    assert not checkbox.isChecked()
    assert window.actions == [True, False]


@pytest.mark.parametrize("name", ["line", "plain"])
def test_text_inputs_insert_space_and_preserve_repeats(window, name):
    editor = window.tasks.task_input if name == "line" else window.editor
    focus(editor)
    QTest.keyPress(editor, Qt.Key.Key_Space)
    repeat_space(editor)
    QTest.keyRelease(editor, Qt.Key.Key_Space)
    text = editor.text() if name == "line" else editor.toPlainText()
    assert text == "  "
    assert window.actions == []


def test_global_space_activates_primary_once_per_physical_press(window):
    focus(window)
    QTest.keyPress(window, Qt.Key.Key_Space)
    assert window.actions == ["primary"]
    repeat_space(window)
    QTest.keyRelease(window, Qt.Key.Key_Space)
    assert window.actions == ["primary"]
    space(window)
    assert window.actions == ["primary", "primary"]


def test_focus_transitions_route_each_key_to_current_control(window):
    focus(window)
    space(window)
    focus(window.timer.finish_button)
    space(window.timer.finish_button)
    focus(window.tasks.task_input)
    space(window.tasks.task_input)
    focus(window.timer.cancel_button)
    space(window.timer.cancel_button)
    focus(window)
    space(window)
    assert window.tasks.task_input.text() == " "
    assert window.actions == ["primary", "finish", "cancel", "primary"]


def test_disabled_button_cannot_keep_shortcut_routing_stale(window):
    button = window.timer.finish_button
    focus(button)
    space(button)
    button.setEnabled(False)
    focus(window)
    button.setFocus()
    assert QApplication.focusWidget() is window
    space(window)
    button.setEnabled(True)
    focus(button)
    space(button)
    assert window.actions == ["finish", "primary", "finish"]


def test_focus_cleared_restores_global_space(window):
    focus(window.timer.cancel_button)
    space(window.timer.cancel_button)
    window.timer.cancel_button.clearFocus()
    assert QApplication.focusWidget() is None
    space(window)
    assert window.actions == ["cancel", "primary"]
