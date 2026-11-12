"""R2-B6 / DSK-03: rendered contrast of the Desktop palette, light and dark.

Style of Android's DarkThemeTextContrastTest: build the production window
offscreen, grab its pixels, and compare the resolved text colour or the
focus ring pixels with the rendered surface next to them. WCAG AA limits:
4.5:1 for text, 3:1 for focus rings and other non-text accents.

Item views (task table, arrivals list) draw no focus ring at all; that is
DSK-04 and stays outside this file.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPalette
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QStyle,
    QStyleOptionButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.timer_view import ClockWidget
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore

TEXT_LIMIT = 4.5
NON_TEXT_LIMIT = 3.0
OLD_CORAL = "#ff604f"
SCREENS = ("timer", "tasks", "arrivals", "network")
RING_CONTROLS = (QAbstractButton, QLineEdit, QComboBox, QPlainTextEdit, ClockWidget)

# Fusion's standard light palette and the dark palette of the UX audit
# harness, pinned so the host OS theme cannot change the result.
PALETTE_ROLES = {
    "light": {
        "Window": "#efefef", "WindowText": "#000000", "Base": "#ffffff",
        "AlternateBase": "#f7f7f7", "Text": "#000000", "Button": "#efefef",
        "ButtonText": "#000000", "Mid": "#b8b8b8", "Midlight": "#cacaca",
        "Dark": "#9f9f9f", "Light": "#ffffff", "Shadow": "#767676",
    },
    "dark": {
        "Window": "#1e1f22", "WindowText": "#e6e6e6", "Base": "#2b2d30",
        "AlternateBase": "#323438", "Text": "#e6e6e6", "Button": "#3a3c40",
        "ButtonText": "#e6e6e6", "Mid": "#5a5c60", "Midlight": "#44464a",
        "Dark": "#101113", "Light": "#5e6064", "Shadow": "#000000",
    },
}


@dataclass(frozen=True)
class RenderedPair:
    label: str
    foreground: QColor
    surface: QColor

    @property
    def ratio(self) -> float:
        return contrast(self.foreground, self.surface)

    def describe(self) -> str:
        return (
            f"{self.label}: {self.foreground.name()} on "
            f"{self.surface.name()} = {self.ratio:.2f}:1"
        )


@dataclass(frozen=True)
class FocusRing:
    label: str
    ring: QColor | None  # None: focus changed no pixel of the control
    outside: QColor
    unfocused_border: QColor

    def against_outside(self) -> float:
        return contrast(self.ring, self.outside) if self.ring else 1.0

    def against_unfocused_border(self) -> float:
        return contrast(self.ring, self.unfocused_border) if self.ring else 1.0

    def describe(self) -> str:
        ring = self.ring.name() if self.ring else "no ring"
        return (
            f"{self.label}: ring {ring} on {self.outside.name()} = "
            f"{self.against_outside():.2f}:1, unfocused border "
            f"{self.unfocused_border.name()} = "
            f"{self.against_unfocused_border():.2f}:1"
        )


def _relative_luminance(color: QColor) -> float:
    def linear(channel: float) -> float:
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * linear(color.redF())
        + 0.7152 * linear(color.greenF())
        + 0.0722 * linear(color.blueF())
    )


def contrast(first: QColor, second: QColor) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication, rounds: int = 8) -> None:
    for _ in range(rounds):
        app.processEvents()


@contextmanager
def _application_palette(app: QApplication, name: str) -> Iterator[None]:
    previous = QPalette(QApplication.palette())
    palette = QPalette()
    for role, colour in PALETTE_ROLES[name].items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(colour))
    QApplication.setPalette(palette)
    _process(app)
    try:
        yield
    finally:
        QApplication.setPalette(previous)
        _process(app)


@contextmanager
def _main_window(app: QApplication, tmp_path) -> Iterator[MainWindow]:
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, FakeCloud(authenticated=True), QIcon(), locale="en")
    window.tick_timer.stop()
    window.sync_timer.stop()
    tasks = [{"id": f"task-{i}", "title": f"Task {i}"} for i in range(3)]
    window.tasks = tasks
    window.tasks_screen.render(tasks, [], mutations_enabled=True)
    window.resize(780, 760)
    window.show()
    _process(app, 20)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


@pytest.fixture(params=("light", "dark"))
def themed_window(request, app, tmp_path) -> Iterator[MainWindow]:
    with _application_palette(app, request.param):
        with _main_window(app, tmp_path) as window:
            yield window


def _is_dark(window: QWidget) -> bool:
    return window.palette().color(QPalette.ColorRole.Window).lightnessF() < 0.5


def _open_screen(app: QApplication, window: MainWindow, screen: str) -> QWidget:
    window.navigation.screen_requested.emit(SCREENS.index(screen))
    _process(app, 20)
    if screen == "tasks":
        window.tasks_screen.undo_banner.present("Task 1")
        _process(app, 20)
    return window.page_stack.currentWidget()


def _grab(app: QApplication, window: QWidget) -> QImage:
    _process(app)
    return window.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)


def _rect_in_window(window: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def _pixel_counts(image: QImage, rect: QRect) -> Counter[int]:
    visible = rect.intersected(image.rect())
    return Counter(
        image.pixel(x, y)
        for y in range(visible.top(), visible.bottom() + 1)
        for x in range(visible.left(), visible.right() + 1)
    )


def _most_common(counts: Counter[int]) -> QColor:
    return QColor(counts.most_common(1)[0][0])


def _label(widget: QWidget) -> str:
    text = widget.text() if hasattr(widget, "text") else ""
    return f"{type(widget).__name__}#{widget.objectName()} {text[:24]!r}"


def _shown(widget: QWidget) -> bool:
    return (
        widget.isVisible()
        and widget.isEnabled()
        and not widget.visibleRegion().isEmpty()
    )


def _strongest_glyph_colour(counts: Counter[int], surface: QColor) -> QColor:
    glyph_colours = [QColor(rgb) for rgb, count in counts.items() if count >= 3]
    return max(glyph_colours, key=lambda colour: contrast(colour, surface))


def _rendered_text(window: QWidget, image: QImage, widget: QWidget) -> RenderedPair:
    """The widget's resolved text colour against the surface it rendered."""
    counts = _pixel_counts(image, _rect_in_window(window, widget))
    surface = _most_common(counts)
    resolved = widget.palette().color(widget.foregroundRole())
    if counts[resolved.rgb()] == 0:
        # A pseudo-state rule (checked tab) or thin antialiased glyphs keep
        # the palette colour out of the render; read the glyphs instead.
        resolved = _strongest_glyph_colour(counts, surface)
    return RenderedPair(_label(widget), resolved, surface)


def _text_pairs(app: QApplication, window: QWidget) -> list[RenderedPair]:
    image = _grab(app, window)
    widgets = window.findChildren(QLabel) + window.findChildren(QAbstractButton)
    return [
        _rendered_text(window, image, widget)
        for widget in widgets
        if _shown(widget) and widget.text().strip()
    ]


def _clear_focus(app: QApplication, window: QWidget) -> None:
    focused = window.focusWidget()
    if focused is not None:
        focused.clearFocus()
    _process(app)


def _focus_ring(app: QApplication, window: QWidget, control: QWidget) -> FocusRing:
    """Pixels of the control that keyboard focus repaints, and their neighbours."""
    _clear_focus(app, window)
    unfocused = _grab(app, window)
    control.setFocus(Qt.FocusReason.TabFocusReason)
    focused = _grab(app, window)
    _clear_focus(app, window)
    rect = _rect_in_window(window, control).intersected(focused.rect())
    ring_pixels: Counter[int] = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            if unfocused.pixel(x, y) != focused.pixel(x, y):
                ring_pixels[focused.pixel(x, y)] += 1
    outside = _pixel_counts(focused, rect.adjusted(-3, -3, 3, 3))
    outside -= _pixel_counts(focused, rect)
    border = _pixel_counts(unfocused, rect)
    border -= _pixel_counts(unfocused, rect.adjusted(1, 1, -1, -1))
    return FocusRing(
        _label(control),
        _most_common(ring_pixels) if ring_pixels else None,
        _most_common(outside),
        _most_common(border),
    )


def _ring_controls(scope: QWidget) -> list[QWidget]:
    return [
        widget
        for widget in scope.findChildren(QWidget)
        if isinstance(widget, RING_CONTROLS)
        and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
        and _shown(widget)
    ]


def _weak_rings(app: QApplication, window: QWidget, controls: list[QWidget]) -> list[str]:
    rings = [_focus_ring(app, window, control) for control in controls]
    return [
        ring.describe() for ring in rings if ring.against_outside() < NON_TEXT_LIMIT
    ]


def _named_button(page: QWidget, object_name: str) -> QWidget:
    return next(
        button
        for button in page.findChildren(QAbstractButton)
        if button.objectName() == object_name and _shown(button)
    )


@pytest.mark.parametrize("screen", SCREENS)
def test_text_is_readable_on_its_rendered_surface(app, themed_window, screen) -> None:
    _open_screen(app, themed_window, screen)
    pairs = _text_pairs(app, themed_window)
    assert len(pairs) >= 6, [pair.describe() for pair in pairs]
    unreadable = [pair.describe() for pair in pairs if pair.ratio < TEXT_LIMIT]
    assert not unreadable, "\n".join(unreadable)


# DSK-11: the danger red used to read at 2.13:1 on the dark button. The
# Network danger buttons show only after sign-in and share the same rule.
@pytest.mark.parametrize("screen", ("timer", "tasks"))
def test_danger_labels_are_readable(app, themed_window, screen) -> None:
    _open_screen(app, themed_window, screen)
    if screen == "timer":
        _start_focus(app, themed_window)
    danger = [
        pair
        for pair in _text_pairs(app, themed_window)
        if "#dangerButton" in pair.label
    ]
    assert danger
    weak = [pair.describe() for pair in danger if pair.ratio < TEXT_LIMIT]
    assert not weak, "\n".join(weak)


def _start_focus(app: QApplication, window: MainWindow) -> None:
    # Cancel, the Timer danger button, shows only while a timer runs.
    window.timer_screen.primary_button.click()
    _process(app, 20)
    assert window.timer_screen.cancel_button.isVisible()


@pytest.mark.parametrize("checked", (False, True), ids=("unchecked", "checked"))
def test_auto_start_indicator_contrasts_with_its_surface(
    app, themed_window, checked
) -> None:
    box = _shown_auto_start_box(app, themed_window)
    box.setChecked(checked)
    _process(app)
    indicator = _indicator_rect(themed_window, box)
    image = _grab(app, themed_window)
    inside = _pixel_counts(image, indicator)
    outside = _pixel_counts(image, indicator.adjusted(-3, -3, 3, 3)) - inside
    surface = _most_common(outside)
    edge = inside - _pixel_counts(image, indicator.adjusted(1, 1, -1, -1))
    border = _most_common(edge)
    assert contrast(border, surface) >= NON_TEXT_LIMIT, (border.name(), surface.name())
    marks = [QColor(rgb) for rgb in inside if QColor(rgb) not in (border, surface)]
    if checked:
        # The checked fill is the state cue, so it needs 3:1 of its own.
        fill = max(marks, key=lambda colour: inside[colour.rgb()])
        assert contrast(fill, surface) >= NON_TEXT_LIMIT, (fill.name(), surface.name())
    else:
        assert not marks, [mark.name() for mark in marks]


def _shown_auto_start_box(app: QApplication, window: MainWindow) -> QAbstractButton:
    _open_screen(app, window, "timer")
    window.settings_button.click()
    _process(app, 20)
    box = window.timer_screen.auto_breaks
    window.timer_screen.settings_scroll.ensureWidgetVisible(box)
    _process(app)
    assert _shown(box)
    return box


def _indicator_rect(window: QWidget, box: QAbstractButton) -> QRect:
    option = QStyleOptionButton()
    box.initStyleOption(option)
    rect = box.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator, option, box
    )
    return QRect(box.mapTo(window, rect.topLeft()), rect.size())


# Arrivals has no focusable control besides its list (DSK-04).
@pytest.mark.parametrize("screen", ("timer", "tasks", "network"))
def test_focus_ring_contrasts_with_the_surface_around_it(
    app, themed_window, screen
) -> None:
    page = _open_screen(app, themed_window, screen)
    controls = _ring_controls(page)
    assert controls
    weak = _weak_rings(app, themed_window, controls)
    assert not weak, "\n".join(weak)


def test_header_focus_rings_contrast_with_the_band(app, themed_window) -> None:
    page_stack = themed_window.page_stack
    controls = [
        control
        for control in _ring_controls(themed_window)
        if not page_stack.isAncestorOf(control)
    ]
    assert len(controls) >= 5, [_label(control) for control in controls]
    weak = _weak_rings(app, themed_window, controls)
    assert not weak, "\n".join(weak)


@pytest.mark.parametrize(
    ("screen", "object_name"),
    [("timer", "primaryButton"), ("tasks", "primaryButton"), ("tasks", "dangerButton")],
)
def test_focus_recolours_primary_and_danger_borders(
    app, themed_window, screen, object_name
) -> None:
    """DSK-03: these buttons kept their own border colour when focused."""
    page = _open_screen(app, themed_window, screen)
    ring = _focus_ring(app, themed_window, _named_button(page, object_name))
    assert ring.against_outside() >= NON_TEXT_LIMIT, ring.describe()
    assert ring.against_unfocused_border() >= NON_TEXT_LIMIT, ring.describe()


def test_progress_arc_contrasts_with_the_dial_face(app, themed_window) -> None:
    clock = themed_window.clock
    rect = _rect_in_window(themed_window, clock)
    clock.set_state("25:00", "focus", "running", 0.0)
    empty = _grab(app, themed_window)
    clock.set_state("25:00", "focus", "running", 0.5)
    half = _grab(app, themed_window)
    arc_pixels: Counter[int] = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            if empty.pixel(x, y) != half.pixel(x, y):
                arc_pixels[half.pixel(x, y)] += 1
    face = clock.palette().color(QPalette.ColorRole.AlternateBase)
    assert _pixel_counts(half, rect)[face.rgb()] > 0, "dial face is not rendered"
    arc = RenderedPair("progress arc", _most_common(arc_pixels), face)
    assert arc.ratio >= NON_TEXT_LIMIT, arc.describe()


def test_progress_arc_contrasts_with_the_track_ring(app, themed_window) -> None:
    """B6 question 2: the arc is drawn over the track ring, so it needs 3:1 there."""
    clock = themed_window.clock
    rect = _rect_in_window(themed_window, clock)
    clock.set_state("25:00", "focus", "running", 0.0)
    empty = _grab(app, themed_window)
    clock.set_state("25:00", "focus", "running", 0.5)
    half = _grab(app, themed_window)
    arc_pixels: Counter[int] = Counter()
    under_arc: Counter[int] = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            if empty.pixel(x, y) != half.pixel(x, y):
                arc_pixels[half.pixel(x, y)] += 1
                under_arc[empty.pixel(x, y)] += 1
    face = clock.palette().color(QPalette.ColorRole.AlternateBase)
    track = _most_common(under_arc)
    # The arc covers the ring, not the face; otherwise this measures nothing.
    assert track != face, f"track ring {track.name()} is the dial face"
    pair = RenderedPair("progress arc on the track ring", _most_common(arc_pixels), track)
    assert pair.ratio >= NON_TEXT_LIMIT, pair.describe()


def test_privacy_notice_accent_contrasts_with_the_notice(app, themed_window) -> None:
    page = _open_screen(app, themed_window, "network")
    notice = next(
        label
        for label in page.findChildren(QLabel)
        if label.objectName() == "privacyNotice" and _shown(label)
    )
    image = _grab(app, themed_window)
    rect = _rect_in_window(themed_window, notice)
    leading_edge = QRect(rect.topLeft(), QPoint(rect.left() + 1, rect.bottom()))
    accent = RenderedPair(
        "privacy notice accent",
        _most_common(_pixel_counts(image, leading_edge)),
        _most_common(_pixel_counts(image, rect)),
    )
    assert accent.ratio >= NON_TEXT_LIMIT, accent.describe()


@contextmanager
def _styled_field(app: QApplication, sheet: str) -> Iterator[tuple[QWidget, QLineEdit]]:
    host = QWidget()
    layout = QVBoxLayout(host)
    field = QLineEdit()
    caption = QLabel("Departures")
    caption.setObjectName("caption")
    layout.addWidget(caption)
    layout.addWidget(field)
    host.setStyleSheet(sheet)
    host.show()
    _process(app)
    try:
        yield host, field
    finally:
        host.close()
        host.deleteLater()
        _process(app)


def test_measurement_rejects_the_old_coral_on_a_light_surface(app) -> None:
    """Control: the checks fail for the colours DSK-03 reported."""
    sheet = (
        "QLineEdit { border: 2px solid palette(mid); }"
        "QLineEdit:focus { border: 3px solid %s; }"
        "QLabel#caption { color: #8fa8b8; }"
    )
    with _application_palette(app, "light"):
        with _styled_field(app, sheet % OLD_CORAL) as (host, field):
            old_ring = _focus_ring(app, host, field)
            caption = _text_pairs(app, host)[0]
        with _styled_field(app, sheet % "#000000") as (host, field):
            black_ring = _focus_ring(app, host, field)
    assert old_ring.ring == QColor(OLD_CORAL), old_ring.describe()
    assert old_ring.against_outside() < NON_TEXT_LIMIT, old_ring.describe()
    assert black_ring.against_outside() >= NON_TEXT_LIMIT, black_ring.describe()
    assert caption.ratio < TEXT_LIMIT, caption.describe()
