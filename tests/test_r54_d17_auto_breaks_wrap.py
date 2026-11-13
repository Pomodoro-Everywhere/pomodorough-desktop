"""R54-D17: the auto-start check box wraps instead of widening the window.

At 20 pt in the ar-XB pseudo-locale the one-line "Auto-start breaks" label
was the widest child of the Timer settings column. With DejaVu Sans, the
font of the Ubuntu runner, the settings column, the clock and the controls
then needed more than an 800-pixel screen, and the D76 cap on the window
minimum cut the Timer page off. The check box now breaks its label between
words when the column is narrower than the one-line label.

The screen-cap case itself lives in test_r2_c_m3_timer_width.py. Every
expectation here is measured from the widgets; no pixel constant is used.
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QWidget,
)
from test_r2_c_m3_timer_width import _timer_window
from test_r52_d05_nav_reflow import _process, _scaled_font


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def _painted_label(check_box: QCheckBox) -> str:
    painted: list[QStyleOptionButton] = []

    def record(painter, element, option):
        if element == QStyle.ControlElement.CE_CheckBox:
            painted.append(QStyleOptionButton(option))

    with patch.object(QStylePainter, "drawControl", record):
        check_box.repaint()
    assert painted, "the check box did not paint through the style"
    return painted[-1].text


def _rect_in(widget: QWidget, ancestor: QWidget) -> QRect:
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


def _settings_shown(app, window) -> QCheckBox:
    window.settings_button.setChecked(True)
    _process(app, 12)
    window.resize(window.minimumSize())
    _process(app, 12)
    check_box = window.timer_screen.auto_breaks
    # The minimum window is short, so the box may sit below the viewport.
    window.timer_screen.settings_scroll.ensureWidgetVisible(check_box)
    _process(app, 12)
    return check_box


def test_a_narrow_auto_start_label_wraps_on_whole_words(app, tmp_path):
    with _scaled_font(app, 20.0):
        with _timer_window(app, tmp_path, "ar-XB", "minimum") as window:
            check_box = _settings_shown(app, window)
            one_line = QCheckBox.sizeHint(check_box)
            assert check_box.width() < one_line.width()
            # The box asks for its widest word, not the one-line label.
            assert check_box.minimumSizeHint().width() < one_line.width()
            assert check_box.height() >= check_box.heightForWidth(check_box.width())
            assert check_box.heightForWidth(check_box.width()) > one_line.height()
            lines = _painted_label(check_box).split("\n")
            assert len(lines) > 1
            # Breaks fall between words only; the accessible text is unchanged.
            assert " ".join(lines).split() == check_box.text().split()
            assert "\n" not in check_box.text()
            metrics = check_box.fontMetrics()
            chrome = one_line.width() - metrics.horizontalAdvance(check_box.text())
            for line in lines:
                if len(line.split()) > 1:
                    assert metrics.horizontalAdvance(line) <= check_box.width() - chrome, line
            # The wrapped box stays inside the settings column; it never
            # needs the horizontal scroll bar.
            viewport = window.timer_screen.settings_scroll.viewport()
            assert viewport.rect().contains(_rect_in(check_box, viewport))


def test_a_click_on_the_last_wrapped_line_toggles_the_box(app, tmp_path):
    with _scaled_font(app, 20.0):
        with _timer_window(app, tmp_path, "ar-XB", "minimum") as window:
            check_box = _settings_shown(app, window)
            option = QStyleOptionButton()
            check_box.initStyleOption(option)
            option.text = _painted_label(check_box)
            label = check_box.style().subElementRect(
                QStyle.SubElement.SE_CheckBoxFocusRect, option, check_box
            )
            last_line = QPoint(label.center().x(), label.bottom() - 1)
            assert check_box.rect().contains(last_line)
            toggled: list[bool] = []
            check_box.toggled.connect(toggled.append)
            before = check_box.isChecked()
            QTest.mouseClick(check_box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, last_line)
            assert check_box.isChecked() is not before
            assert toggled == [not before]


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_auto_start_label_keeps_one_line_while_it_fits(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _timer_window(app, tmp_path, locale, "default") as window:
            check_box = _settings_shown(app, window)
            one_line = QCheckBox.sizeHint(check_box)
            assert check_box.width() >= one_line.width()
            assert check_box.heightForWidth(check_box.width()) == one_line.height()
            assert check_box.height() == one_line.height()
