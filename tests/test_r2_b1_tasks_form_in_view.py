"""UX round 2 B1 residual: the Tasks form scrolls into view.

B1 keeps the 600x340 minimum and the one-row header, so at large fonts the
Tasks header labels push the Add button under the page viewport. Opening
Tasks, and moving focus into the form, scroll the form into view instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from test_r2_b1_navigation_menu import _rect_in, _shrink_to_minimum
from test_r52_d05_nav_reflow import _main_window, _process, _scaled_font

CASES = pytest.mark.parametrize(
    ("locale", "points"), [("en", 24.0), ("ar-XB", 18.0)], ids=["en-24", "ar-XB-18"]
)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _add_button_in_view(window) -> bool:
    viewport = window.tasks_page.viewport()
    add = _rect_in(window.tasks_screen.add_task_button, viewport)
    return viewport.rect().contains(add)


@CASES
def test_opening_tasks_scrolls_the_add_button_into_view(app, tmp_path, locale, points):
    with _scaled_font(app, points), _main_window(app, tmp_path, locale, 640, 360) as window:
        _shrink_to_minimum(app, window)
        window.navigation.screen_requested.emit(1)
        _process(app)
        assert window.page_stack.currentWidget() is window.tasks_page
        assert _add_button_in_view(window), window.tasks_page.verticalScrollBar().value()


@CASES
def test_focus_entering_the_form_scrolls_it_into_view(app, tmp_path, locale, points):
    with _scaled_font(app, points), _main_window(app, tmp_path, locale, 640, 360) as window:
        _shrink_to_minimum(app, window)
        window.navigation.screen_requested.emit(1)
        _process(app)
        bar = window.tasks_page.verticalScrollBar()
        for widget in (window.tasks_screen.task_input, window.tasks_screen.add_task_button):
            window.navigation.setFocus(Qt.FocusReason.OtherFocusReason)
            bar.setValue(bar.maximum())
            _process(app)
            # The page bottom hides the form, so the reveal must scroll back.
            assert not _add_button_in_view(window), bar.maximum()
            widget.setFocus(Qt.FocusReason.MouseFocusReason)
            _process(app)
            assert widget.hasFocus()
            assert _add_button_in_view(window), widget.objectName()
