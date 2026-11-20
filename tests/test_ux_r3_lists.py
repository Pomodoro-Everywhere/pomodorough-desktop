"""UX round 3: Tasks board, task picker, Arrivals list and Network page.

DSK-04 visible focus and no invisible Tab stops, DSK-06 picker title,
DSK-09 wrapping arrivals, DSK-13 large fonts, DSK-16 rename discoverability
and merge confirmation, DSK-18 arrivals count and dated times.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from PySide6.QtCore import QDateTime, QLocale, QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)
from test_r52_d06_task_header_wrap import _process, _scaled_font, _tasks_window

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.task_rename_merge import build_merge_dialog
from pomodorough.tasks_screen import TasksScreen

LOCALES = pytest.mark.parametrize("locale", ["en", "ar-XB"])
LONG_TITLE = "Write the quarterly planning document for the platform team"
COMPLETED_AT = "2026-10-27T10:00:00Z"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _arrival(index: int, task_id: str | None = None) -> dict:
    return {
        "id": f"arrival-{index}",
        "timerId": f"timer-{index}",
        "taskId": task_id,
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "elapsedMs": 1_500_000,
        "completedAt": COMPLETED_AT,
    }


def _changed_pixels(first: QImage, second: QImage) -> int:
    return sum(
        first.pixel(x, y) != second.pixel(x, y)
        for y in range(first.height())
        for x in range(first.width())
    )


@pytest.fixture
def lists_host(app):
    """Tasks and Arrivals screens beside a line edit that can take focus away."""
    host = QWidget()
    layout = QVBoxLayout(host)
    host.elsewhere = QLineEdit()
    layout.addWidget(host.elsewhere)
    host.tasks = TasksScreen(Strings("en"))
    host.arrivals = ArrivalsScreen(Strings("en"), "device-12345678")
    layout.addWidget(host.tasks)
    layout.addWidget(host.arrivals)
    host.setStyleSheet(TasksScreen.stylesheet() + ArrivalsScreen.stylesheet())
    host.resize(700, 800)
    host.show()
    host.activateWindow()
    tasks = [task_from_title(f"Task {row}") for row in range(3)]
    host.tasks.render(tasks, [], mutations_enabled=True)
    host.arrivals.render([_arrival(index) for index in range(3)], {})
    _process(app)
    yield host
    host.close()
    host.deleteLater()
    _process(app)


def _focus(app, widget: QWidget) -> None:
    widget.setFocus()
    _process(app)


# DSK-04 ---------------------------------------------------------------------


def test_focused_task_board_shows_its_focus(app, lists_host):
    table = lists_host.tasks.task_table
    _focus(app, lists_host.elsewhere)
    unfocused = table.grab().toImage()
    _focus(app, table)
    assert app.focusWidget() is table
    assert _changed_pixels(unfocused, table.grab().toImage()) > 0


def test_focused_arrivals_list_shows_focus_and_current_row(app, lists_host):
    listing = lists_host.arrivals.history_list
    _focus(app, lists_host.elsewhere)
    unfocused = listing.grab().toImage()
    _focus(app, listing)
    focused = listing.grab().toImage()
    assert _changed_pixels(unfocused, focused) > 0
    QTest.keyClick(listing, Qt.Key.Key_Down)
    _process(app)
    assert listing.currentRow() == 1
    assert _changed_pixels(focused, listing.grab().toImage()) > 0
    # R54-D02: the list still selects nothing; only the current row shows.
    assert listing.selectedItems() == []


def test_focused_arrivals_list_without_arrivals_shows_its_focus(app, lists_host):
    """No row can be current in an empty list, so the list border marks focus."""
    listing = lists_host.arrivals.history_list
    lists_host.arrivals.render([], {})
    _focus(app, lists_host.elsewhere)
    unfocused = listing.grab().toImage()
    _focus(app, listing)
    assert app.focusWidget() is listing
    assert listing.currentItem() is None
    assert _changed_pixels(unfocused, listing.grab().toImage()) > 0


def test_empty_arrivals_row_is_not_a_keyboard_target(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([], {})
    item = screen.history_list.item(0)
    assert item is not None
    assert item.flags() == Qt.ItemFlag.NoItemFlags
    screen.deleteLater()


@LOCALES
def test_page_scroll_areas_are_not_tab_stops(app, tmp_path, locale):
    with _tasks_window(app, tmp_path, locale, 800, 600) as window:
        for index in range(len(window.screen_buttons)):
            window._display_screen(index)
            _process(app)
            stops = [
                widget.objectName() or type(widget).__name__
                for widget in window.findChildren(QScrollArea)
                if widget.isVisible() and widget.focusPolicy() & Qt.FocusPolicy.TabFocus
            ]
            assert stops == [], (index, stops)


# DSK-06 ---------------------------------------------------------------------


def _add_tasks(app, window, titles: list[str]) -> None:
    for title in titles:
        window.task_input.setText(title)
        window.add_task_button.click()
        _process(app)


def _picker_image(app, window, title: str) -> QImage:
    combo = window.task_combo
    combo.setCurrentIndex(combo.findText(title))
    _process(app)
    return combo.grab().toImage()


def test_picker_shows_the_start_of_a_long_title_with_an_ellipsis(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 600, 400) as window:
        window._display_screen(0)
        _add_tasks(app, window, [LONG_TITLE])
        combo = window.task_combo
        combo.setCurrentIndex(combo.findText(LONG_TITLE))
        _process(app)
        assert combo.fontMetrics().horizontalAdvance(LONG_TITLE) > combo.width()
        long_image = combo.grab().toImage()
        # The closed picker paints exactly what the elided title paints alone.
        shown = _shown_title(combo)
        _add_tasks(app, window, [shown])
        assert shown.startswith(LONG_TITLE[:3]) and shown.endswith("…"), shown
        assert _changed_pixels(long_image, _picker_image(app, window, shown)) == 0


def _shown_title(combo) -> str:
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    field = combo.style().subControlRect(
        QStyle.ComplexControl.CC_ComboBox,
        option,
        QStyle.SubControl.SC_ComboBoxEditField,
        combo,
    )
    return combo.fontMetrics().elidedText(
        LONG_TITLE, Qt.TextElideMode.ElideRight, field.width()
    )


def test_picker_tooltip_names_the_whole_title(app, tmp_path):
    hint = "Applies to the current running focus timer and the next timer."
    with _tasks_window(app, tmp_path, "en", 600, 400) as window:
        window._display_screen(0)
        _add_tasks(app, window, [LONG_TITLE])
        combo = window.task_combo
        index = combo.findText(LONG_TITLE)
        combo.setCurrentIndex(index)
        _process(app)
        assert combo.toolTip() == f"{LONG_TITLE}\n{hint}"
        assert combo.accessibleDescription() == hint
        assert combo.itemData(index, Qt.ItemDataRole.ToolTipRole) == LONG_TITLE
        combo.setCurrentIndex(0)
        _process(app)
        assert combo.toolTip() == hint


def test_picker_width_follows_the_font(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 600, 400) as window:
        panel = window.timer_screen.task_selector_panel
        small = window.task_combo.minimumSizeHint().width()
        assert panel.minimumWidth() == 0
        with _scaled_font(app, QApplication.font().pointSizeF() * 2):
            large = window.task_combo.minimumSizeHint().width()
        assert large > small


# DSK-09 and DSK-13 -----------------------------------------------------------


@LOCALES
def test_arrivals_rows_wrap_without_a_sideways_bar(app, tmp_path, locale):
    with _tasks_window(app, tmp_path, locale, 600, 340) as window:
        window._show_screen(2)
        window.arrivals_screen.render(
            [_arrival(0, "long")], {"long": {"title": LONG_TITLE * 3}}
        )
        _process(app, 30)
        listing = window.arrivals_screen.history_list
        rect = listing.visualItemRect(listing.item(0))
        assert listing.wordWrap()
        assert listing.horizontalScrollBar().maximum() == 0
        assert rect.width() <= listing.viewport().width(), rect
        # The row holds a line break; a wrapped title adds at least one line.
        assert rect.height() >= 3 * listing.fontMetrics().lineSpacing(), rect


def _clipped_labels(root: QWidget) -> list[str]:
    clipped = []
    for label in root.findChildren(QLabel):
        if not label.isVisible() or not label.text():
            continue
        needed = (
            label.heightForWidth(label.width())
            if label.wordWrap()
            else label.sizeHint().height()
        )
        if label.height() < needed:
            clipped.append(f"{label.text()!r} {label.height()} < {needed}")
    return clipped


def _outside_sideways(scroll: QScrollArea) -> list[str]:
    """Visible page widgets that reach past the left or right viewport edge.

    A page whose sideways bar is hidden or has no range cannot bring such a
    widget back, so a height check alone passes while the widget is cut.
    """
    viewport = scroll.viewport()
    outside = []
    for child in scroll.widget().findChildren(QWidget):
        if not child.isVisible():
            continue
        left = child.mapTo(viewport, QPoint(0, 0)).x()
        right = left + child.width()
        if left < 0 or right > viewport.width():
            name = child.objectName() or type(child).__name__
            outside.append(f"{name} {left}..{right} of {viewport.width()}")
    return outside


def _whole_when_scrolled_to(scroll: QScrollArea, widget: QWidget) -> bool:
    scroll.ensureWidgetVisible(widget, 0, 0)
    return widget.visibleRegion().boundingRect() == widget.rect()


# The minimum window and the default window (ui.py: setMinimumSize, resize).
WINDOW_SIZES = pytest.mark.parametrize("size", [(600, 340), (640, 360)])


@LOCALES
@WINDOW_SIZES
def test_arrivals_header_is_whole_at_twice_the_font(app, tmp_path, locale, size):
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 2),
        _tasks_window(app, tmp_path, locale, *size) as window,
    ):
        window._show_screen(2)
        window.arrivals_screen.render([_arrival(index) for index in range(3)], {})
        _process(app, 30)
        page = window.arrivals_screen
        assert _clipped_labels(page) == []
        assert page._scroll.widget().width() <= page._scroll.viewport().width()
        assert page._scroll.horizontalScrollBar().maximum() == 0
        assert _outside_sideways(page._scroll) == []
        assert _whole_when_scrolled_to(page._scroll, page.history_count)
        assert _whole_when_scrolled_to(page._scroll, page.device_label)


def _count_badge_beside_title(page) -> bool:
    title = page.findChild(QLabel, "sectionTitle").geometry()
    return page.history_count.geometry().top() < title.bottom()


@LOCALES
def test_arrivals_count_moves_under_the_title_only_while_the_row_is_too_wide(
    app, tmp_path, locale
):
    """The font grows after the first layout; the header follows it both ways."""
    with _tasks_window(app, tmp_path, locale, 600, 340) as window:
        window._show_screen(2)
        window.arrivals_screen.render([_arrival(index) for index in range(3)], {})
        _process(app, 30)
        page = window.arrivals_screen
        assert _count_badge_beside_title(page)
        with _scaled_font(app, QApplication.font().pointSizeF() * 3):
            _process(app, 30)
            assert not _count_badge_beside_title(page)
            # Stacked, the badge keeps its own width at the leading edge.
            assert page.history_count.width() == page.history_count.sizeHint().width()
            assert _whole_when_scrolled_to(page._scroll, page.history_count)
        _process(app, 30)
        assert _count_badge_beside_title(page)
        assert _outside_sideways(page._scroll) == []


@contextmanager
def _arrivals_page(
    app, count: int, direction: Qt.LayoutDirection = Qt.LayoutDirection.LeftToRight
):
    """The page by itself: no window minimum, so any width can be tested."""
    page = ArrivalsScreen(Strings("en"), "device-12345678")
    page.setLayoutDirection(direction)
    page.setStyleSheet(ArrivalsScreen.stylesheet())
    page.render([_arrival(index) for index in range(count)], {})
    # Tall, so the list never adds an up-and-down bar that narrows the page.
    page.resize(page.sizeHint().width(), page.screen().availableGeometry().height())
    page.show()
    _process(app, 30)
    try:
        yield page
    finally:
        page.close()
        page.deleteLater()
        _process(app)


def _header_room(page) -> int:
    """Width the page gives its header now."""
    margins = page._scroll.widget().layout().contentsMargins()
    return page._scroll.viewport().width() - margins.left() - margins.right()


def _one_row_header_width(page) -> int:
    """Width the title beside the count badge needs, from the widgets' own
    hints: the widest shown heading label, the gap, and the badge."""
    heading = (
        page.findChild(QLabel, "sectionTitle"),
        page.history_scope,
        page.sync_hint,
    )
    widest = max(
        label.minimumSizeHint().width() for label in heading if not label.isHidden()
    )
    badge = page.history_count.minimumSizeHint().width()
    return widest + page._header.spacing() + badge


def _leave_header_room(app, page, spare: int) -> None:
    """Resize the page so the one-row header has `spare` px left over."""
    width = page.width() - _header_room(page) + _one_row_header_width(page) + spare
    page.resize(width, page.height())
    _process(app, 30)


def _assert_count_badge_stacked_on_the_page(page) -> None:
    scroll = page._scroll
    assert not _count_badge_beside_title(page)
    assert scroll.widget().width() <= scroll.viewport().width()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert _outside_sideways(scroll) == []


def test_arrivals_count_moves_under_the_title_at_the_exact_page_width(app):
    with _arrivals_page(app, 3) as page:
        _leave_header_room(app, page, 0)
        assert _count_badge_beside_title(page)

        _leave_header_room(app, page, -1)
        assert not _count_badge_beside_title(page)
        assert page._scroll.horizontalScrollBar().maximum() == 0
        assert _outside_sideways(page._scroll) == []

        _leave_header_room(app, page, 0)
        assert _count_badge_beside_title(page)


def test_arrivals_count_moves_under_the_title_when_the_page_narrows_in_two_steps(app):
    """A drag passes through the width where the row just fits.

    From there the scroll area keeps the page at its minimum width, so the
    page gets no resize of its own; only the viewport tells it to reflow.
    The page is short, as at a large font: it already scrolls up and down,
    so a sideways bar that comes and goes does not resize it either.
    """
    with _arrivals_page(app, 3) as page:
        page.resize(page.width(), page._scroll.widget().minimumSizeHint().height() // 2)
        _process(app, 30)
        assert page._scroll.verticalScrollBar().maximum() > 0
        _leave_header_room(app, page, 0)
        assert _count_badge_beside_title(page)
        assert _header_room(page) == _one_row_header_width(page)

        _leave_header_room(app, page, -page.history_count.width())
        _assert_count_badge_stacked_on_the_page(page)

        _leave_header_room(app, page, 0)
        assert _count_badge_beside_title(page)


def test_arrivals_header_follows_a_window_narrowed_through_the_exact_width(
    app, tmp_path
):
    """The reviewed case: ar-XB at twice the font, narrowed to the minimum window."""
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 2),
        _tasks_window(app, tmp_path, "ar-XB", 760, 340) as window,
    ):
        window._show_screen(2)
        window.arrivals_screen.render([_arrival(index) for index in range(3)], {})
        _process(app, 30)
        page = window.arrivals_screen
        exact = window.width() - _header_room(page) + _one_row_header_width(page)
        assert window.minimumWidth() < exact < window.width()
        window.resize(exact, window.height())
        _process(app, 30)
        assert _header_room(page) == _one_row_header_width(page)
        assert _count_badge_beside_title(page)

        window.resize(window.minimumSize())
        _process(app, 30)
        assert window.size() == window.minimumSize()
        _assert_count_badge_stacked_on_the_page(page)
        assert _whole_when_scrolled_to(page._scroll, page.history_count)


@pytest.mark.parametrize(
    "direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft]
)
def test_stacked_arrivals_count_sits_at_the_leading_edge_under_the_title(
    app, direction
):
    with _arrivals_page(app, 3, direction) as page:
        _leave_header_room(app, page, -1)
        _assert_count_badge_stacked_on_the_page(page)
        title = page.findChild(QLabel, "sectionTitle").geometry()
        badge = page.history_count.geometry()
        assert badge.width() == page.history_count.sizeHint().width()
        assert badge.width() < title.width()
        if direction == Qt.LayoutDirection.LeftToRight:
            assert badge.left() == title.left()
        else:
            assert badge.right() == title.right()


def test_arrivals_count_returns_beside_the_title_when_its_text_gets_shorter(app):
    """Only the badge text changes: no resize tells the page to reflow."""
    with _arrivals_page(app, 1) as page:
        _leave_header_room(app, page, 0)
        assert _count_badge_beside_title(page)

        page.render([_arrival(index) for index in range(12)], {})
        _process(app, 30)
        assert not _count_badge_beside_title(page)
        assert _outside_sideways(page._scroll) == []

        page.render([_arrival(0)], {})
        _process(app, 30)
        assert _count_badge_beside_title(page)


def test_arrivals_page_shows_a_sideways_bar_when_one_title_outgrows_it(app, tmp_path):
    """Past any reflow the bar is the last resort; the overflow is never hidden."""
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 5),
        _tasks_window(app, tmp_path, "ar-XB", 600, 340) as window,
    ):
        window._show_screen(2)
        _process(app, 30)
        scroll = window.arrivals_screen._scroll
        assert scroll.widget().width() > scroll.viewport().width()
        assert scroll.horizontalScrollBar().maximum() > 0
        assert scroll.horizontalScrollBar().isVisible()


@LOCALES
def test_arrivals_page_does_not_scroll_at_the_default_font(app, tmp_path, locale):
    """The list gives up height before the page scrolls: one scroll bar, not two."""
    with _tasks_window(app, tmp_path, locale, 600, 340) as window:
        window._show_screen(2)
        window.arrivals_screen.render([_arrival(index) for index in range(9)], {})
        _process(app, 30)
        screen = window.arrivals_screen
        assert screen._scroll.verticalScrollBar().maximum() == 0
        assert screen.history_list.verticalScrollBar().maximum() > 0
        assert screen.device_label.visibleRegion().boundingRect().height() > 0


@LOCALES
def test_arrivals_list_keeps_room_for_its_rows_at_twice_the_font(app, tmp_path, locale):
    """The page scrolls at a large font, so the list is not crushed to one row."""
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 2),
        _tasks_window(app, tmp_path, locale, 600, 340) as window,
    ):
        window._show_screen(2)
        window.arrivals_screen.render([_arrival(index) for index in range(9)], {})
        _process(app, 30)
        screen, listing = window.arrivals_screen, window.arrivals_screen.history_list
        assert screen._scroll.verticalScrollBar().maximum() > 0
        assert listing.viewport().height() >= 5 * listing.fontMetrics().lineSpacing()


@LOCALES
def test_network_page_never_scrolls_sideways_at_twice_the_font(app, tmp_path, locale):
    with (
        _scaled_font(app, QApplication.font().pointSizeF() * 2),
        _tasks_window(app, tmp_path, locale, 600, 340) as window,
    ):
        window._show_screen(3)
        _process(app, 30)
        page = window.network_screen
        assert page._scroll.horizontalScrollBar().maximum() == 0
        assert _clipped_labels(page._content) == []


def _account_buttons_side_by_side(page) -> bool:
    privacy = page.privacy_policy_button.geometry()
    delete = page.delete_account_button.geometry()
    return privacy.top() == delete.top()


def test_network_rows_return_to_one_line_when_room_comes_back(app, tmp_path):
    with _tasks_window(app, tmp_path, "en", 600, 340) as window:
        window._show_screen(3)
        page = window.network_screen
        with _scaled_font(app, QApplication.font().pointSizeF() * 2):
            _process(app, 30)
            assert not _account_buttons_side_by_side(page)
            assert page._scroll.horizontalScrollBar().maximum() == 0
        _process(app, 30)
        window.resize(1200, 800)
        _process(app, 30)
        assert _account_buttons_side_by_side(page)
        combo = page.replication_mode_combo.geometry()
        route = page.replication_mode_combo.parentWidget().findChildren(QLabel)
        assert any(
            label.text() == page.strings.text("network.route")
            and label.geometry().center().y() in range(combo.top(), combo.bottom())
            for label in route
        )


def _room_actions_side_by_side(page) -> bool:
    refresh = page.refresh_invite_button.geometry()
    sync = page.sync_iroh_button.geometry()
    return refresh.top() == sync.top()


def test_network_room_actions_stack_when_the_panel_cannot_hold_them(app, tmp_path):
    """A little past twice the font the two room buttons are wider than the panel.

    Linux fonts reach that width at twice the font already. The buttons stack
    then, so the page does not scroll sideways, and return to one line later.
    """
    with _tasks_window(app, tmp_path, "ar-XB", 600, 340) as window:
        window._show_screen(3)
        page = window.network_screen
        with _scaled_font(app, QApplication.font().pointSizeF() * 2.1):
            _process(app, 30)
            assert not _room_actions_side_by_side(page)
            assert page._scroll.horizontalScrollBar().maximum() == 0
            assert _clipped_labels(page._content) == []
        _process(app, 30)
        assert _room_actions_side_by_side(page)


def test_network_route_name_wider_than_the_page_does_not_scroll_it_sideways(
    app, tmp_path
):
    """The route selector narrows to the page and ends the name with an
    ellipsis. Its least width followed the name, and on Linux fonts the
    pseudo-locale name at twice the font widened the page past its view.

    The font grows until the name is wider than the page on the host.
    """
    with _tasks_window(app, tmp_path, "ar-XB", 600, 340) as window:
        window._show_screen(3)
        page = window.network_screen
        combo = page.replication_mode_combo
        base = QApplication.font().pointSizeF()
        for scale in (2.1, 2.5, 3.0):
            with _scaled_font(app, base * scale):
                _process(app, 30)
                margins = page._content.layout().contentsMargins()
                room = page._scroll.viewport().width() - margins.left() - margins.right()
                if combo.minimumSizeHint().width() <= room:
                    continue
                # Other controls may widen the page at this font; the route
                # row must not be one of them.
                assert page._mode_row.minimumSize().width() <= room
                return
        pytest.skip("the host font keeps the route name inside the page")


# DSK-16 ---------------------------------------------------------------------


@pytest.fixture
def board(app):
    view = TasksScreen(Strings("en"))
    view.resize(480, 360)
    view.show()
    view.activateWindow()
    view.render(
        [task_from_title("Write"), task_from_title(LONG_TITLE)],
        [],
        mutations_enabled=True,
    )
    _process(app)
    renamed: list[tuple[str, str]] = []
    view.rename_task_requested.connect(
        lambda task, title: renamed.append((task, title))
    )
    view.renamed = renamed
    yield view
    view.close()
    view.deleteLater()
    _process(app)


def _rename(app, view: TasksScreen, row: int, title: str) -> QLineEdit:
    table = view.task_table
    table.setFocus()
    table.setCurrentCell(row, 0)
    _process(app)
    QTest.keyClick(table, Qt.Key.Key_F2)
    _process(app)
    editor = table.cellWidget(row, 0)
    assert isinstance(editor, QLineEdit)
    accessible_name = editor.accessibleName()
    editor.selectAll()
    QTest.keyClicks(editor, title)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    _process(app)
    return accessible_name


def test_title_cells_show_the_whole_title_and_how_to_rename(app, board):
    item = board.task_table.item(1, 0)
    assert item.toolTip() == f"{LONG_TITLE}\nDouble-click or press F2 to rename."
    assert board.task_table.accessibleDescription() == (
        "Double-click a task name, press F2, or open its context menu to rename it."
    )


def test_rename_editor_has_an_accessible_name(app, board):
    with patch("pomodorough.tasks_screen.confirm_rename_merge", create=True) as confirm:
        assert _rename(app, board, 0, "Read") == "Rename Write"
    confirm.assert_not_called()
    assert board.renamed == [(task_from_title("Write")["id"], "Read")]


@pytest.mark.parametrize("merge", [False, True], ids=["cancel", "merge"])
def test_rename_onto_an_existing_title_asks_first(app, board, merge):
    # create=True lets the scenario run against a board without the
    # confirmation, where the rename goes through unasked.
    with patch(
        "pomodorough.tasks_screen.confirm_rename_merge",
        return_value=merge,
        create=True,
    ) as confirm:
        _rename(app, board, 0, LONG_TITLE)
    expected = [(task_from_title("Write")["id"], LONG_TITLE)] if merge else []
    assert board.renamed == expected
    confirm.assert_called_once_with(board, board.strings, "Write", LONG_TITLE)
    assert board.task_table.item(0, 0).text() == "Write"


def test_merge_dialog_names_both_tasks_and_defaults_to_cancel(app):
    parent = QWidget()
    dialog, merge_button = build_merge_dialog(
        parent, Strings("en"), "Write", LONG_TITLE
    )
    assert f"“{LONG_TITLE}”" in dialog.text() and "“Write”" in dialog.text()
    assert LONG_TITLE in dialog.informativeText()
    assert merge_button.text() == "Merge"
    assert dialog.defaultButton() is dialog.escapeButton()
    assert dialog.defaultButton() is not merge_button
    assert dialog.buttonRole(merge_button) == QMessageBox.ButtonRole.AcceptRole
    parent.deleteLater()


# DSK-18 ---------------------------------------------------------------------


@pytest.mark.parametrize(("count", "expected"), [(1, "1 ARRIVAL"), (3, "3 ARRIVALS")])
def test_arrivals_count_names_the_total_once(app, count, expected):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([_arrival(index) for index in range(count)], {})
    assert screen.history_count.text() == expected
    noun = "arrival" if count == 1 else "arrivals"
    assert screen.history_list.accessibleDescription().startswith(
        f"{count} retained {noun}:"
    )
    screen.deleteLater()


def test_arrival_time_includes_the_date(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([_arrival(0)], {})
    moment = QDateTime.fromString(COMPLETED_AT, Qt.DateFormat.ISODate).toLocalTime()
    date = QLocale().toString(moment.date(), QLocale.FormatType.ShortFormat)
    assert date in screen.history_list.item(0).text()
    screen.deleteLater()
