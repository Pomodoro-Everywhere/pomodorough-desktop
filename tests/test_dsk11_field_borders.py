"""DSK-11: field and panel borders and placeholder text, light and dark.

Palette Mid drew input, list, table and panel borders at 1.7:1 to 2.5:1
against the surfaces they separate, and Qt's half-alpha placeholder text
was 4.0:1 on the field base. The rendered border must reach the WCAG
non-text limit (3:1) against both sides, and the placeholder the text
limit (4.5:1) against the field.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import test_r2_b6_palette_contrast as palette_contrast
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSpinBox,
    QTableWidget,
    QWidget,
)
from test_r2_b6_palette_contrast import (
    NON_TEXT_LIMIT,
    TEXT_LIMIT,
    _grab,
    _open_screen,
    _rect_in_window,
    contrast,
)

from pomodorough.railway_palette import control_border
from pomodorough.tasks_screen import TasksScreen
from pomodorough.ui import MainWindow

# Light and dark application palettes, shared with the R2-B6 contrast tests.
app = palette_contrast.app
themed_window = palette_contrast.themed_window

BORDERED_FRAMES = ("controlsPanel", "taskSelector", "networkPanel", "undoBanner")
BORDERED_FIELDS = (QComboBox, QLineEdit, QPlainTextEdit, QListWidget, QTableWidget)


def _bordered_widgets(page: QWidget) -> list[QWidget]:
    widgets = [
        widget
        for kind in BORDERED_FIELDS
        for widget in page.findChildren(kind)
        if not isinstance(widget, QLineEdit) or not widget.parent().inherits("QComboBox")
    ]
    widgets += [
        frame
        for frame in page.findChildren(QFrame)
        if frame.objectName() in BORDERED_FRAMES
    ]
    return [w for w in widgets if w.isVisible() and w.width() > 8]


def _left_edge(image: QImage, rect: QRect) -> tuple[QColor, QColor, QColor]:
    """Outside, border and inside pixels across the left edge, mid height."""
    y = rect.center().y()
    x = rect.left()
    return (
        QColor(image.pixel(x - 1, y)),
        QColor(image.pixel(x, y)),
        QColor(image.pixel(x + 4, y)),
    )


@pytest.mark.parametrize("screen", ["timer", "tasks", "arrivals", "network"])
def test_field_and_panel_borders_reach_three_to_one(
    app, themed_window: MainWindow, screen: str
) -> None:
    page = _open_screen(app, themed_window, screen)
    image = _grab(app, themed_window)
    widgets = _bordered_widgets(page)
    assert widgets, screen

    failures = []
    for widget in widgets:
        rect = _rect_in_window(themed_window, widget)
        if rect.left() < 1:
            continue
        outside, border, inside = _left_edge(image, rect)
        ratios = (contrast(border, outside), contrast(border, inside))
        if min(ratios) < NON_TEXT_LIMIT:
            failures.append(
                f"{type(widget).__name__}#{widget.objectName()} border "
                f"{border.name()} outside {outside.name()} inside {inside.name()} "
                f"{ratios[0]:.2f}/{ratios[1]:.2f}"
            )
    assert not failures, failures


# DSK-21: the settings spin boxes used the bare platform style beside the
# styled fields. They now carry the same border as every other field.
def test_settings_spin_boxes_share_the_field_border(app, themed_window) -> None:
    _open_screen(app, themed_window, "timer")
    themed_window.settings_button.click()
    spins = [
        spin for spin in themed_window.timer_screen.findChildren(QSpinBox) if spin.isVisible()
    ]
    image = _grab(app, themed_window)
    field_border = QColor(control_border(themed_window.palette()))

    assert len(spins) == 3
    for spin in spins:
        rect = _rect_in_window(themed_window, spin)
        outside, border, _ = _left_edge(image, rect)
        # Mid height, a few pixels in, the digits can start where the style
        # gives the spin box less padding (PySide6 6.11.3). The row just
        # under the top border is clear of text.
        inside = QColor(image.pixel(rect.left() + 4, rect.top() + 2))
        assert border == field_border, (border.name(), field_border.name())
        assert contrast(border, outside) >= NON_TEXT_LIMIT, (border.name(), outside.name())
        assert contrast(border, inside) >= NON_TEXT_LIMIT, (border.name(), inside.name())


def test_placeholder_text_reaches_text_contrast(app, themed_window) -> None:
    _open_screen(app, themed_window, "tasks")
    field = themed_window.tasks_screen.task_input
    field.clear()
    themed_window.setFocus()
    image = _grab(app, themed_window)
    rect = _rect_in_window(themed_window, field).adjusted(4, 4, -4, -4)
    base = field.palette().color(field.backgroundRole())
    glyphs = [
        QColor(image.pixel(x, y))
        for y in range(rect.top(), rect.bottom())
        for x in range(rect.left(), rect.right())
    ]
    strongest = max(glyphs, key=lambda colour: contrast(colour, base))

    assert field.placeholderText()
    assert contrast(strongest, base) >= TEXT_LIMIT, (strongest.name(), base.name())


# A focused button draws its ring in the text colour. The first Tasks row's
# DELETE button sits against the table border, so the border must not come
# within 3:1 of the ring while it clears 3:1 against the surfaces.
@pytest.mark.parametrize("name", ["light", "dark"])
def test_border_stays_clear_of_surfaces_and_of_the_focus_ring(app, name) -> None:
    with palette_contrast._application_palette(app, name):
        palette = QApplication.palette()
        border = QColor(control_border(palette))
        surfaces = [
            palette.color(role)
            for role in (
                QPalette.ColorRole.Window,
                QPalette.ColorRole.Base,
                QPalette.ColorRole.AlternateBase,
            )
        ]
        ring = palette.color(QPalette.ColorRole.Text)

    assert min(contrast(border, surface) for surface in surfaces) >= NON_TEXT_LIMIT
    assert contrast(ring, border) >= NON_TEXT_LIMIT, (ring.name(), border.name())


def test_no_stylesheet_uses_palette_mid_for_borders() -> None:
    assert "palette(mid)" not in TasksScreen.stylesheet()
    assert "palette(mid)" not in MainWindow._stylesheet()
    assert "@border" not in MainWindow._stylesheet()
    assert "@placeholder" not in MainWindow._stylesheet()
