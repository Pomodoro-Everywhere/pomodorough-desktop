"""R54-D02: oversized arrival rows are fully readable by keyboard alone.

Every key goes to QApplication.focusWidget(), as a real keyboard sends it.
"""

from __future__ import annotations

import math
from itertools import pairwise, product

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFont, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication
from test_r52_d06_task_header_wrap import (
    _process,
    _scaled_font,
    _tasks_window,
)

TALL_TITLE = "\n".join(["Arrival وصول"] * 8)
SIZES = pytest.mark.parametrize(
    "size", [(640, 360), (600, 340)], ids=["640x360", "600x340"]
)
LOCALES = pytest.mark.parametrize("locale", ["en", "ar-XB"])
PLAIN = Qt.KeyboardModifier.NoModifier
CONTROL = Qt.KeyboardModifier.ControlModifier
DOWNWARD = (Qt.Key.Key_Down, Qt.Key.Key_PageDown)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _arrival(prefix, index, task_id=None, pending=False):
    return {
        "id": f"{prefix}-{index}",
        "timerId": f"timer-{prefix}-{index}",
        "taskId": task_id,
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "completedAt": "2026-10-27T10:00:00Z",
        "pending": pending,
    }


def _scenario(position):
    """Return history, tasks, and the tall row; "sole" is the audit's one row.

    The audit's row had no task title and was taller than the view only
    under the stacked navigation header. The one-row header leaves room for
    that row at 18 pt, so the sole row carries the tall title too.
    """
    if position == "sole":
        return [_arrival(position, 0, "tall")], {"tall": {"title": TALL_TITLE}}, 0
    count, tall = (6, 5) if position == "last" else (5, 2)
    history = [
        _arrival(position, index, "tall" if index == tall else None)
        for index in range(count)
    ]
    return history, {"tall": {"title": TALL_TITLE}}, tall


def _show(app, window, history, tasks):
    window._show_screen(2)
    window.arrivals_screen.render(history, tasks)
    _process(app, 30)
    listing = window.arrivals_screen.history_list
    listing.setCurrentRow(0)
    listing.verticalScrollBar().setValue(0)
    _process(app)
    assert app.focusWidget() is listing
    return listing


def _press(app, key, modifier=PLAIN):
    QTest.keyClick(app.focusWidget(), key, modifier)
    _process(app)


def _band(listing, row):
    rect = listing.visualItemRect(listing.item(row))
    top = max(0, -rect.top())
    bottom = min(rect.height(), listing.viewport().height() - rect.top())
    return (top, bottom) if bottom > top else None


def _covers(bands, height):
    edge = 0
    for top, bottom in sorted(band for band in bands if band):
        if top > edge:
            return False
        edge = max(edge, bottom)
    return edge >= height


def _walk(app, listing, key, tall, modifier=PLAIN):
    """Press key until neither the current row nor the scroll offset moves."""
    bar = listing.verticalScrollBar()
    state = (listing.currentRow(), bar.value())
    bands, rows = [_band(listing, tall)], [state[0]]
    for _ in range(bar.maximum() + listing.count() + 2):
        _press(app, key, modifier)
        bands.append(_band(listing, tall))
        rows.append(listing.currentRow())
        if (listing.currentRow(), bar.value()) == state:
            break
        state = (listing.currentRow(), bar.value())
    return bands, rows


def _assert_walks_cover(app, listing, tall, label):
    height = listing.visualItemRect(listing.item(tall)).height()
    assert height > listing.viewport().height(), label
    last = listing.count() - 1
    bar = listing.verticalScrollBar()
    for forward, backward in (
        (Qt.Key.Key_Down, Qt.Key.Key_Up),
        (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp),
    ):
        bands, rows = _walk(app, listing, forward, tall)
        assert _covers(bands, height), (label, forward, bands)
        assert listing.currentRow() == last, (label, forward, rows)
        if forward == Qt.Key.Key_Down:
            assert rows == sorted(rows), (label, rows)
            assert sorted(set(rows)) == list(range(last + 1)), (label, rows)
        bands, rows = _walk(app, listing, backward, tall)
        assert _covers(bands, height), (label, backward, bands)
        assert (listing.currentRow(), bar.value()) == (0, 0), (label, rows)


def _assert_boundary_keys(app, listing, label):
    bar = listing.verticalScrollBar()
    last = listing.count() - 1
    _press(app, Qt.Key.Key_End)
    rect = listing.visualItemRect(listing.item(last))
    assert listing.currentRow() == last, label
    assert bar.value() == bar.maximum() > 0, label
    assert 0 <= rect.bottom() < listing.viewport().height(), (label, rect)
    _press(app, Qt.Key.Key_Home)
    assert listing.currentRow() == 0, label
    assert bar.value() == bar.minimum() == 0, label
    assert listing.visualItemRect(listing.item(0)).top() == 0, label


def _assert_contract_kept(app, listing, tall):
    item = listing.item(tall)
    assert app.focusWidget() is listing
    assert listing.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    assert listing.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert listing.selectedItems() == []
    accessible = item.data(Qt.ItemDataRole.AccessibleTextRole)
    assert accessible == item.text().replace("\n", ", ")


@LOCALES
@SIZES
@pytest.mark.parametrize("points", [18.0, 30.0])
def test_keyboard_reads_whole_tall_row(app, tmp_path, locale, size, points):
    rtl = Qt.LayoutDirection.RightToLeft
    with (
        _scaled_font(app, points),
        _tasks_window(app, tmp_path, locale, *size) as window,
    ):
        for position in ("sole", "last", "middle"):
            history, tasks, tall = _scenario(position)
            listing = _show(app, window, history, tasks)
            assert (listing.layoutDirection() == rtl) == (locale == "ar-XB")
            label = f"{position} viewport={listing.viewport().height()}"
            _assert_walks_cover(app, listing, tall, label)
            _assert_boundary_keys(app, listing, label)
            _assert_contract_kept(app, listing, tall)


@LOCALES
def test_control_keys_read_whole_tall_row(app, tmp_path, locale):
    """Acceptance also pressed Ctrl+Down and Ctrl+End; both must read the row."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 600, 340) as window,
    ):
        for position in ("sole", "last", "middle"):
            history, tasks, tall = _scenario(position)
            listing = _show(app, window, history, tasks)
            height = listing.visualItemRect(listing.item(tall)).height()
            # Without a row taller than the view the walk below proves nothing.
            assert height > listing.viewport().height(), position
            bands, rows = _walk(app, listing, Qt.Key.Key_Down, tall, CONTROL)
            assert _covers(bands, height), (position, bands)
            assert listing.currentRow() == listing.count() - 1, (position, rows)
            _press(app, Qt.Key.Key_Home, CONTROL)
            _press(app, Qt.Key.Key_End, CONTROL)
            bar = listing.verticalScrollBar()
            assert listing.currentRow() == listing.count() - 1, position
            assert bar.value() == bar.maximum() > 0, position


def _set_points(app, points):
    font = QFont(app.font())
    font.setPointSizeF(points)
    app.setFont(font)
    _process(app, 30)


def _read_into_middle(app, listing, row):
    height = listing.viewport().height()
    for _ in range(listing.verticalScrollBar().maximum() + listing.count()):
        rect = listing.visualItemRect(listing.item(row))
        if listing.currentRow() == row and rect.top() < 0 and rect.bottom() >= height:
            return
        _press(app, Qt.Key.Key_Down)
    pytest.fail("keyboard never reached the middle of the tall row")


def _changed_renders(history):
    """Each change lies at or below tall row 3, so its top must not move."""
    appended = [*history, _arrival("render", len(history))]
    pending = [dict(item, pending=index == 5) for index, item in enumerate(appended)]
    retitled = {"tall": {"title": TALL_TITLE + " Arrival"}}
    tasks = {"tall": {"title": TALL_TITLE}}
    return ((appended, tasks), (pending, tasks), (pending, retitled))


@LOCALES
@SIZES
def test_changed_render_keeps_mid_row_position(app, tmp_path, locale, size):
    history = [_arrival("render", i, "tall" if i == 3 else None) for i in range(8)]
    with _scaled_font(app, 24.0), _tasks_window(app, tmp_path, locale, *size) as window:
        listing = _show(app, window, history, {"tall": {"title": TALL_TITLE}})
        _read_into_middle(app, listing, 3)
        bar = listing.verticalScrollBar()
        before = (bar.value(), listing.visualItemRect(listing.item(3)).top())
        assert before[0] > 0 > before[1]
        for changed, tasks in _changed_renders(history):
            signature = window.arrivals_screen._render_signature
            window.arrivals_screen.render(changed, tasks)
            _process(app, 30)
            assert window.arrivals_screen._render_signature != signature
            current = listing.currentItem()
            assert current.data(Qt.ItemDataRole.UserRole) == "render-3"
            top = listing.visualItemRect(listing.item(3)).top()
            assert (bar.value(), top) == before
            assert app.focusWidget() is listing


@LOCALES
@pytest.mark.parametrize("position", ["sole", "last"])
def test_live_font_growth_and_shrink(app, tmp_path, locale, position):
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        history, tasks, tall = _scenario(position)
        listing = _show(app, window, history, tasks)
        heights = []
        for points in (18.0, 30.0, 18.0):
            _set_points(app, points)
            _press(app, Qt.Key.Key_End)
            rect = listing.visualItemRect(listing.item(tall))
            assert rect.height() > listing.viewport().height()
            assert 0 <= rect.bottom() < listing.viewport().height()
            heights.append(rect.height())
        assert heights[1] > heights[0]
        assert heights[2] == heights[0]


@LOCALES
def test_short_rows_keep_row_navigation(app, tmp_path, locale):
    history = [_arrival("short", index) for index in range(30)]
    with _tasks_window(app, tmp_path, locale, 640, 360) as window:
        listing = _show(app, window, history, {})
        bar = listing.verticalScrollBar()
        height = listing.viewport().height()
        for row in range(1, listing.count()):
            _press(app, Qt.Key.Key_Down)
            rect = listing.visualItemRect(listing.currentItem())
            assert listing.currentRow() == row
            assert 0 <= rect.top() and rect.bottom() < height
        _press(app, Qt.Key.Key_Home)
        _press(app, Qt.Key.Key_Down)
        rect = listing.visualItemRect(listing.item(1))
        assert listing.currentRow() == 1
        assert 0 <= rect.top() and rect.bottom() < height
        # Clip the current short row's top, as a wheel scroll would; Up still
        # moves to the previous row instead of scrolling within the current one.
        bar.setValue(bar.value() + rect.top() + 1)
        _press(app, Qt.Key.Key_Up)
        rect = listing.visualItemRect(listing.item(0))
        assert listing.currentRow() == 0
        assert 0 == rect.top() and rect.bottom() < height
        _press(app, Qt.Key.Key_End)
        assert listing.currentRow() == listing.count() - 1
        assert bar.value() == bar.maximum()


def _sized_tasks(app, window, viewports=1.6):
    """Return the tall task, its row sized from the viewport as the probe sizes it."""
    window._show_screen(2)
    _process(app, 30)
    listing = window.arrivals_screen.history_list
    spacing = listing.fontMetrics().lineSpacing()
    lines = max(8, math.ceil(viewports * listing.viewport().height() / spacing) + 2)
    return {"tall": {"title": "\n".join(["Arrival وصول"] * lines)}}


def _sized_history(count, tall):
    return [
        _arrival("sized", index, "tall" if index == tall else None)
        for index in range(count)
    ]


def _show_sized_tall_row(app, window, count, tall, viewports=1.6):
    """Show count rows; row tall is sized from the viewport as the probe sizes it."""
    tasks = _sized_tasks(app, window, viewports)
    return _show(app, window, _sized_history(count, tall), tasks)


def _page_walk(app, listing, key, modifier=PLAIN):
    """Page until nothing moves; return the bands each row showed on the way."""
    bar = listing.verticalScrollBar()
    rows = range(listing.count())
    bands = [[_band(listing, row)] for row in rows]
    state = (listing.currentRow(), bar.value())
    height = listing.viewport().height()
    for _ in range(bar.maximum() + listing.count() + 2):
        _press(app, key, modifier)
        current = listing.currentRow()
        rect = listing.visualItemRect(listing.item(current))
        assert _band(listing, current), (key, current, rect)
        if rect.height() <= height:
            assert listing.viewport().rect().contains(rect), (key, current, rect)
        elif current != state[0]:
            # A tall row is entered at its near end, as Down and Up enter it.
            near = rect.top() if key == Qt.Key.Key_PageDown else rect.bottom() + 1
            assert near == (0 if key == Qt.Key.Key_PageDown else height), (key, rect)
        for row in rows:
            bands[row].append(_band(listing, row))
        if (current, bar.value()) == state:
            return bands
        state = (current, bar.value())
    pytest.fail(f"{key} never settled")


def _assert_every_row_read(listing, bands, key):
    for row, shown in enumerate(bands):
        height = listing.visualItemRect(listing.item(row)).height()
        assert _covers(shown, height), (key, row, shown)


def _assert_pages_both_ways(app, listing, modifier=PLAIN):
    bar = listing.verticalScrollBar()
    last = listing.count() - 1
    bands = _page_walk(app, listing, Qt.Key.Key_PageDown, modifier)
    _assert_every_row_read(listing, bands, Qt.Key.Key_PageDown)
    assert (listing.currentRow(), bar.value()) == (last, bar.maximum())
    assert listing.visualItemRect(listing.item(last)).bottom() < (
        listing.viewport().height()
    )
    bands = _page_walk(app, listing, Qt.Key.Key_PageUp, modifier)
    _assert_every_row_read(listing, bands, Qt.Key.Key_PageUp)
    assert (listing.currentRow(), bar.value()) == (0, 0)


def _assert_mixed_heights(listing):
    height = listing.viewport().height()
    fits = {
        listing.visualItemRect(listing.item(row)).height() <= height
        for row in range(listing.count())
    }
    assert fits == {True, False}


@LOCALES
@pytest.mark.parametrize("tall", [0, 2, 5], ids=["first", "middle", "last"])
@pytest.mark.parametrize("modifier", [PLAIN, CONTROL], ids=["plain", "ctrl"])
def test_paging_reads_rows_of_mixed_heights(app, tmp_path, locale, tall, modifier):
    """F1: Qt paging stalled beside a tall row among rows that fit the viewport."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show_sized_tall_row(app, window, 6, tall)
        _assert_mixed_heights(listing)
        _assert_pages_both_ways(app, listing, modifier)


def test_paging_moves_through_short_rows_right_to_left(app, tmp_path):
    """F5: stock paging never left row 0 of a small ar-XB list at the default font."""
    history = [_arrival("short", index) for index in range(30)]
    with _tasks_window(app, tmp_path, "ar-XB", 600, 340) as window:
        listing = _show(app, window, history, {})
        assert listing.verticalScrollBar().maximum() > listing.viewport().height()
        _assert_pages_both_ways(app, listing)


def _fit_viewport(app, window, listing, height):
    """Resize the window until the viewport is exactly height pixels tall."""
    for _ in range(4):
        gap = height - listing.viewport().height()
        if gap == 0:
            return
        window.resize(window.width(), window.height() + gap)
        _process(app, 30)
    pytest.fail(f"the viewport never reached a height of {height}")


def _viewport_tall_tasks(app, window):
    """Return a task whose row fills the viewport once the viewport fits it."""
    window._show_screen(2)
    _process(app, 30)
    listing = window.arrivals_screen.history_list
    lines = listing.viewport().height() // listing.fontMetrics().lineSpacing() - 1
    return {"exact": {"title": "\n".join(["Arrival وصول"] * lines)}}


def _rows_as_tall_as_the_view(app, window):
    """Return short rows between rows that fill the viewport once it fits them."""
    history = [_arrival("exact", i, "exact" if i % 2 else None) for i in range(7)]
    return history, _viewport_tall_tasks(app, window)


def _show_fitted(app, window, history, tasks, row):
    """Show the rows, then fit the viewport to the height of row."""
    listing = _show(app, window, history, tasks)
    _fit_viewport(
        app, window, listing, listing.visualItemRect(listing.item(row)).height()
    )
    return listing


def _show_rows_as_tall_as_the_view(app, window):
    """Show short rows between rows exactly as tall as the viewport."""
    return _show_fitted(app, window, *_rows_as_tall_as_the_view(app, window), 1)


@LOCALES
def test_paging_reads_rows_as_tall_as_the_viewport(app, tmp_path, locale):
    """N4: a page skipped part of a row exactly as tall as the viewport."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show_rows_as_tall_as_the_view(app, window)
        bar = listing.verticalScrollBar()
        height, line = listing.viewport().height(), _line_step(listing)
        # One line of row 3 shows at the edge the key pages away from.
        for key, top in (
            (Qt.Key.Key_PageUp, line - height),
            (Qt.Key.Key_PageDown, height - line),
        ):
            listing.setCurrentRow(3)
            bar.setValue(
                bar.value() + listing.visualItemRect(listing.item(3)).top() - top
            )
            _process(app)
            assert listing.visualItemRect(listing.item(3)).top() == top, key
            _press(app, key)
            rect = listing.visualItemRect(listing.item(3))
            assert (listing.currentRow(), rect.top()) == (3, 0), (key, rect)
        _press(app, Qt.Key.Key_Home)
        _assert_pages_both_ways(app, listing)


def _show_viewport_tall_row_below_a_taller_one(app, window):
    """Show a short row, a tall row, a row as tall as the viewport, a short row."""
    tasks = {**_viewport_tall_tasks(app, window), **_sized_tasks(app, window)}
    kinds = (None, "tall", "exact", None)
    history = [_arrival("beside", i, kind) for i, kind in enumerate(kinds)]
    return _show_fitted(app, window, history, tasks, 2)


@LOCALES
@pytest.mark.parametrize("modifier", [PLAIN, CONTROL], ids=["plain", "ctrl"])
def test_paging_up_enters_a_tall_row_above_a_viewport_tall_row(
    app, tmp_path, locale, modifier
):
    """X3: PageUp made a tall row current partway above a row as tall as the view."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show_viewport_tall_row_below_a_taller_one(app, window)
        height = listing.viewport().height()
        tall, exact = (listing.visualItemRect(listing.item(r)).height() for r in (1, 2))
        assert tall > height == exact, (tall, height, exact)
        # Paging up from the end must show the viewport-tall row whole, then
        # enter the taller row above it at its bottom.
        _assert_pages_both_ways(app, listing, modifier)


def _assert_pages_return_to_row(app, listing, forward, backward, end):
    """Page toward end; each page and its reverse must come back to the row.

    A page clamped at the scroll limit cannot tell how far it would have
    moved, so its reverse is not checked; the walk still has to reach end.
    """
    bar = listing.verticalScrollBar()
    for _ in range(bar.maximum() + listing.count() + 2):
        start = (listing.currentRow(), bar.value())
        _press(app, forward)
        paged = (listing.currentRow(), bar.value())
        if paged == start:
            break
        if paged[1] == end[1]:
            continue
        _press(app, backward)
        assert listing.currentRow() == start[0], (forward, start, paged)
        _press(app, forward)
        assert listing.currentRow() == paged[0], (forward, start, paged)
    assert (listing.currentRow(), bar.value()) == end, forward


@LOCALES
@pytest.mark.parametrize("tall", [0, 2, 5], ids=["first", "middle", "last"])
def test_paging_back_returns_to_the_paged_row(app, tmp_path, locale, tall):
    """F3: a page cut through a tall row lands on a row the reverse page finds."""
    with _tasks_window(app, tmp_path, locale, 600, 340) as window:
        listing = _show_sized_tall_row(app, window, 6, tall)
        _assert_mixed_heights(listing)
        bar = listing.verticalScrollBar()
        last = (listing.count() - 1, bar.maximum())
        _assert_pages_return_to_row(
            app, listing, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp, last
        )
        _assert_pages_return_to_row(
            app, listing, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, (0, 0)
        )


def _enter_row(app, listing, row):
    """Press Down until row becomes current, reading through rows above it."""
    for _ in range(listing.verticalScrollBar().maximum() + listing.count()):
        if listing.currentRow() == row:
            return listing.visualItemRect(listing.item(row))
        _press(app, Qt.Key.Key_Down)
    pytest.fail(f"keyboard never reached row {row}")


def _line_step(listing):
    return min(listing.fontMetrics().lineSpacing(), listing.viewport().height())


@LOCALES
@SIZES
def test_font_growth_keeps_current_tall_row_in_view(app, tmp_path, locale, size):
    """F2: rows above grew under a kept scroll value and pushed the row out of view."""
    history = [_arrival("font", i, "tall" if i == 3 else None) for i in range(8)]
    with _scaled_font(app, 18.0), _tasks_window(app, tmp_path, locale, *size) as window:
        listing = _show(app, window, history, {"tall": {"title": TALL_TITLE}})
        assert _enter_row(app, listing, 3).top() == 0
        _set_points(app, 30.0)
        rect = listing.visualItemRect(listing.item(3))
        assert (listing.currentRow(), rect.top()) == (3, 0)
        assert rect.height() > listing.viewport().height()
        _press(app, Qt.Key.Key_Down)
        rect = listing.visualItemRect(listing.item(3))
        assert (listing.currentRow(), rect.top()) == (3, -_line_step(listing))


@LOCALES
def test_font_growth_keeps_read_lines_above_view(app, tmp_path, locale):
    """The lines already read stay above the view after the font grows."""
    # DSK-01: the stacked navigation shows whole at 30 pt, so the window is
    # tall enough to leave the list more than one line under that header.
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 900) as window,
    ):
        listing = _show_sized_tall_row(app, window, 1, 0)
        for _ in range(2):
            _press(app, Qt.Key.Key_Down)
        spacing = listing.fontMetrics().lineSpacing()
        assert _line_step(listing) == spacing
        assert listing.visualItemRect(listing.item(0)).top() == -2 * spacing
        _set_points(app, 30.0)
        spacing = listing.fontMetrics().lineSpacing()
        assert _line_step(listing) == spacing
        assert listing.visualItemRect(listing.item(0)).top() == -2 * spacing
        _press(app, Qt.Key.Key_Down)
        assert listing.visualItemRect(listing.item(0)).top() == -3 * spacing


@LOCALES
def test_font_change_after_reading_on_keeps_the_new_position(app, tmp_path, locale):
    """An anchor held from one font change gives way once the reader moves on."""
    # DSK-01: the stacked navigation shows whole at 30 pt, so the window is
    # tall enough to leave the list more than one line under that header.
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 900) as window,
    ):
        listing = _show_sized_tall_row(app, window, 1, 0)
        _set_points(app, 30.0)
        for _ in range(3):
            _press(app, Qt.Key.Key_Down)
        assert listing.visualItemRect(listing.item(0)).top() == -3 * _line_step(listing)
        _set_points(app, 18.0)
        spacing = listing.fontMetrics().lineSpacing()
        assert listing.visualItemRect(listing.item(0)).top() == -3 * spacing


@LOCALES
def test_scrolled_away_view_survives_render_and_font_change(app, tmp_path, locale):
    """A current row scrolled out of view is not pulled back into it."""
    history = [_arrival("away", index) for index in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show(app, window, history, {})
        bar = listing.verticalScrollBar()
        bar.setValue(bar.maximum())
        _process(app)
        value = bar.value()
        assert _band(listing, 0) is None
        window.arrivals_screen.render([*history, _arrival("away", 30)], {})
        _process(app, 30)
        assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == "away-0"
        assert bar.value() == value
        _set_points(app, 30.0)
        assert listing.currentRow() == 0
        assert _band(listing, 0) is None


def _read_down_to(app, listing, where):
    """Press Down until the current short row reaches the view's bottom or middle."""
    height = listing.viewport().height()
    for _ in range(listing.count()):
        rect = listing.visualItemRect(listing.currentItem())
        if where == "bottom" and listing.verticalScrollBar().value() > 0:
            return rect
        if where == "middle" and rect.top() >= height // 3:
            return rect
        _press(app, Qt.Key.Key_Down)
    pytest.fail(f"keyboard never reached the {where} of the view")


@LOCALES
@pytest.mark.parametrize(
    ("size", "points", "where"),
    [
        ((1024, 700), 24.0, "bottom"),
        # DSK-01: the stacked navigation shows whole at 24 pt, so the window
        # is tall enough for the grown middle row to keep its top.
        ((1024, 900), 24.0, "middle"),
        ((600, 340), 15.0, "bottom"),
    ],
    ids=["bottom", "middle", "outgrown"],
)
def test_font_change_and_its_reverse_keep_short_row_reading(
    app, tmp_path, locale, size, points, where
):
    """N2: the row hid after the font grew and stayed hidden once it shrank back."""
    history = [_arrival("short", index) for index in range(30)]
    with _scaled_font(app, 9.0), _tasks_window(app, tmp_path, locale, *size) as window:
        listing = _show(app, window, history, {})
        bar = listing.verticalScrollBar()
        rect = _read_down_to(app, listing, where)
        row, before = listing.currentRow(), (bar.value(), rect.top())
        _set_points(app, points)
        grown, height = (
            listing.visualItemRect(listing.item(row)),
            listing.viewport().height(),
        )
        grown_row = listing.currentRow()
        _set_points(app, 9.0)
        # The return leg is checked first, so a failure there is reported even
        # when the grown view is wrong too.
        rect = listing.visualItemRect(listing.item(row))
        assert (listing.currentRow(), bar.value(), rect.top()) == (row, *before)
        assert grown_row == row
        if grown.height() > height:
            # The row outgrew the view; its top stays in view and the row
            # fills the view below it.
            assert 0 <= grown.top() < height <= grown.bottom() + 1, grown
        else:
            assert 0 <= grown.top() and grown.bottom() < height, grown
        if where == "middle":
            assert grown.top() == before[1], grown


def _restyle_list(app, window):
    """Restyle the list and back; Qt lays its rows out again after each."""
    listing = window.arrivals_screen.history_list
    for style in ("QListWidget {}", ""):
        listing.setStyleSheet(style)
        _process(app, 6)


def _shrink_window(app, window):
    window.resize(window.width(), window.height() - window.height() // 10)
    _process(app, 30)


@LOCALES
def test_only_a_font_change_reanchors_the_current_row(app, tmp_path, locale):
    """A restyle or a window resize after a font change keeps a scrolled view."""
    history = [_arrival("short", index) for index in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show(app, window, history, {})
        bar = listing.verticalScrollBar()
        _set_points(app, 24.0)
        for change in (_restyle_list, _shrink_window):
            # Clip the current row's top, as a wheel scroll would.
            bar.setValue(1)
            _process(app)
            assert listing.visualItemRect(listing.item(0)).top() == -1, change
            change(app, window)
            assert (listing.currentRow(), bar.value()) == (0, 1), change


@LOCALES
def test_list_font_change_keeps_the_current_row_top(app, tmp_path, locale):
    """A font set on the list alone reanchors the row without a viewport resize."""
    history = [_arrival("short", index) for index in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show(app, window, history, {})
        bar, height = listing.verticalScrollBar(), listing.viewport().height()
        listing.setCurrentRow(20)
        top = height // 3
        bar.setValue(bar.value() + listing.visualItemRect(listing.item(20)).top() - top)
        _process(app)
        assert listing.visualItemRect(listing.item(20)).top() == top
        font = QFont(listing.font())
        font.setPointSizeF(24.0)
        listing.setFont(font)
        _process(app, 30)
        rect = listing.visualItemRect(listing.item(20))
        assert listing.viewport().height() == height
        assert (listing.currentRow(), rect.top()) == (20, top), rect


@LOCALES
def test_list_font_growth_keeps_read_lines_above_view(app, tmp_path, locale):
    """A font set on the list alone still scales the lines read above the view."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show_sized_tall_row(app, window, 1, 0)
        height = listing.viewport().height()
        for _ in range(2):
            _press(app, Qt.Key.Key_Down)
        assert listing.visualItemRect(listing.item(0)).top() == -2 * _line_step(listing)
        font = QFont(listing.font())
        font.setPointSizeF(30.0)
        listing.setFont(font)
        _process(app, 30)
        # No window reflow follows, so the anchor the layout applied for the
        # new line spacing is where the row stays.
        assert listing.viewport().height() == height
        spacing = listing.fontMetrics().lineSpacing()
        assert listing.visualItemRect(listing.item(0)).top() == -2 * spacing


@LOCALES
def test_view_shrunk_to_a_held_row_top_shows_the_row(app, tmp_path, locale):
    """A tall row a font change holds at the view's foot shows once the view ends there."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        tasks, history = _sized_tasks(app, window), _sized_history(30, 20)
        listing = _show(app, window, history, tasks)
        bar = listing.verticalScrollBar()
        listing.setCurrentRow(20)
        top = listing.viewport().height() - _line_step(listing)
        bar.setValue(bar.value() + listing.visualItemRect(listing.item(20)).top() - top)
        _process(app)
        assert listing.visualItemRect(listing.item(20)).top() == top
        _set_points(app, 24.0)
        _fit_viewport(app, window, listing, top)
        rect = listing.visualItemRect(listing.item(20))
        assert (listing.currentRow(), rect.top()) == (20, 0), rect


@LOCALES
def test_reflow_after_a_font_shrink_keeps_the_last_row_whole(app, tmp_path, locale):
    """H7: a resize let Qt move the view before the held anchor was checked."""
    history = [_arrival("short", index) for index in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 800, 500) as window,
    ):
        listing = _show(app, window, history, {})
        bar = listing.verticalScrollBar()
        _press(app, Qt.Key.Key_End)
        assert (listing.currentRow(), bar.value()) == (29, bar.maximum())
        # The shrunk font leaves the view at its new scroll limit, which the
        # window reflow then moves before the viewport takes its new height.
        _set_points(app, 13.0)
        size = window.size()
        for width, height in (
            (size.width(), size.height() - size.height() // 8),
            (size.width() * 11 // 10, size.height() * 11 // 10),
        ):
            window.resize(width, height)
            _process(app, 30)
            rect = listing.visualItemRect(listing.item(29))
            assert listing.currentRow() == 29
            assert listing.viewport().rect().contains(rect), (height, rect)


@LOCALES
def test_tall_row_shortened_by_render_is_shown_whole(app, tmp_path, locale):
    """A tall row read past its top and retitled short shows whole at the view's top."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        # Rows below fill the view, so the scroll limit cannot place the row.
        listing = _show_sized_tall_row(app, window, 16, 3)
        _read_into_middle(app, listing, 3)
        history = [_arrival("sized", i, "tall" if i == 3 else None) for i in range(16)]
        window.arrivals_screen.render(history, {"tall": {"title": "Arrival"}})
        _process(app, 30)
        rect = listing.visualItemRect(listing.item(3))
        height = listing.viewport().height()
        assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == "sized-3"
        assert rect.height() < height
        assert 0 == rect.top() and rect.bottom() < height


def _read_to_row_end(app, listing, row):
    """Page Down until row is current and its bottom meets the view's bottom."""
    bar, height = listing.verticalScrollBar(), listing.viewport().height()
    for _ in range(bar.maximum() + listing.count()):
        rect = listing.visualItemRect(listing.item(row))
        if listing.currentRow() == row and rect.bottom() == height - 1:
            return rect
        before = (listing.currentRow(), bar.value())
        _press(app, Qt.Key.Key_PageDown)
        if (listing.currentRow(), bar.value()) == before:
            break
    pytest.fail(f"Page Down stopped before the end of row {row}")


@LOCALES
def test_tall_row_shortened_by_render_still_fills_the_view(app, tmp_path, locale):
    """A tall row read to its end and retitled shorter, still tall, ends at the foot."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        # Showing the screen renders the empty store, so both titles are sized first.
        shorter = _sized_tasks(app, window)
        tasks = _sized_tasks(app, window, viewports=2.6)
        listing = _show(app, window, _sized_history(16, 3), tasks)
        before = _read_to_row_end(app, listing, 3)
        window.arrivals_screen.render(_sized_history(16, 3), shorter)
        _process(app, 30)
        rect, height = (
            listing.visualItemRect(listing.item(3)),
            listing.viewport().height(),
        )
        assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == "sized-3"
        assert height < rect.height() < before.height()
        assert rect.bottom() == height - 1, rect


def _renders_beside(history, row):
    """A pending acknowledgement below row, one above it, then an append."""
    below = [dict(item, pending=i == row + 1) for i, item in enumerate(history)]
    above = [dict(item, pending=i == row - 1) for i, item in enumerate(history)]
    return (below, above, [*above, _arrival("sized", len(history))])


@LOCALES
def test_render_keeps_a_tall_row_top_where_it_shows(app, tmp_path, locale):
    """N1: a render pulled a clicked tall row's visible top up to the view's top."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        tasks, history = _sized_tasks(app, window), _sized_history(9, 4)
        listing = _show(app, window, history, tasks)
        bar = listing.verticalScrollBar()
        # A click makes the row current without scrolling it to its top. The
        # row's top is put halfway down the view, because the rows above it
        # and the header above the list differ in height by locale and font.
        listing.setCurrentRow(4)
        rect = listing.visualItemRect(listing.item(4))
        bar.setValue(bar.value() + rect.top() - listing.viewport().height() // 2)
        _process(app)
        value, top = bar.value(), listing.visualItemRect(listing.item(4)).top()
        assert 0 < top < listing.viewport().height()
        for changed in _renders_beside(history, 4):
            window.arrivals_screen.render(changed, tasks)
            _process(app, 30)
            assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == "sized-4"
            rect = listing.visualItemRect(listing.item(4))
            assert (bar.value(), rect.top()) == (value, top), changed
        # Down reads one line on, however far down the view the row starts.
        _press(app, Qt.Key.Key_Down)
        rect = listing.visualItemRect(listing.item(4))
        assert (listing.currentRow(), rect.top()) == (4, top - _line_step(listing))


def _click(app, y):
    """Click the focused list y pixels below its top, as a reader picks a row."""
    viewport = app.focusWidget().viewport()
    x = viewport.rect().center().x()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, PLAIN, QPoint(x, y))
    _process(app)


def _wheel_down(app):
    """Turn the mouse wheel one notch toward the end over the focused list."""
    viewport = app.focusWidget().viewport()
    center = QPointF(viewport.rect().center())
    event = QWheelEvent(
        center,
        QPointF(viewport.mapToGlobal(center.toPoint())),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        PLAIN,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)
    _process(app)


def _part_shown(listing, row):
    """Whether row shows in the view, but less of it than the view could hold."""
    band = _band(listing, row)
    rect = listing.visualItemRect(listing.item(row))
    fits = min(rect.height(), listing.viewport().height())
    return band is not None and band[1] - band[0] < fits


def _assert_render_keeps_view(app, window, history, tasks):
    """A pending acknowledgement on another arrival, and back, move nothing."""
    listing = window.arrivals_screen.history_list
    bar, row = listing.verticalScrollBar(), listing.currentRow()
    before = (row, bar.value(), listing.visualItemRect(listing.item(row)).top())
    other = len(history) - 1 if row != len(history) - 1 else 0
    for pending in (other, None):
        changed = [dict(item, pending=i == pending) for i, item in enumerate(history)]
        window.arrivals_screen.render(changed, tasks)
        _process(app, 30)
        rect = listing.visualItemRect(listing.item(row))
        assert (listing.currentRow(), bar.value(), rect.top()) == before, pending
    assert app.focusWidget() is listing


@LOCALES
def test_render_after_click_and_wheel_keeps_the_view(app, tmp_path, locale):
    """F1: a render pulled a current row the wheel left part shown into view."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        history, tasks = _rows_as_tall_as_the_view(app, window)
        listing = _show_fitted(app, window, history, tasks, 1)
        part_shown = []
        for y, notches in product((2, listing.viewport().height() // 2), range(1, 6)):
            _show(app, window, history, tasks)
            _click(app, y)
            for _ in range(notches):
                _wheel_down(app)
            part_shown.append(_part_shown(listing, listing.currentRow()))
            _assert_render_keeps_view(app, window, history, tasks)
        assert any(part_shown), part_shown


@LOCALES
@pytest.mark.parametrize("edge", ["top", "bottom"])
def test_render_keeps_a_sliver_of_the_current_row(app, tmp_path, locale, edge):
    """F1: a render pulled a current row scrolled down to a sliver into view."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        tasks, history = _sized_tasks(app, window), _sized_history(16, 6)
        listing = _show(app, window, history, tasks)
        bar, height = listing.verticalScrollBar(), listing.viewport().height()
        # A tall and a short row, each left one pixel or one line in view.
        for row, shown in product((6, 8), (1, _line_step(listing))):
            rect = _enter_row(app, listing, row)
            top = shown - rect.height() if edge == "top" else height - shown
            bar.setValue(bar.value() + rect.top() - top)
            _process(app)
            assert listing.visualItemRect(listing.item(row)).top() == top, row
            _assert_render_keeps_view(app, window, history, tasks)


@LOCALES
def test_render_keeps_a_wheeled_tall_row_where_it_ends(app, tmp_path, locale):
    """F1: a render pulled a wheeled tall row's bottom down to the view's foot."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        tasks, history = _sized_tasks(app, window), _sized_history(9, 4)
        listing = _show(app, window, history, tasks)
        bar, height = listing.verticalScrollBar(), listing.viewport().height()
        assert _enter_row(app, listing, 4).top() == 0
        ends_above_foot = []
        for _ in range(listing.count()):
            value = bar.value()
            _wheel_down(app)
            rect = listing.visualItemRect(listing.item(4))
            if bar.value() == value or rect.bottom() < 0:
                break
            ends_above_foot.append(rect.bottom() < height - 1)
            _assert_render_keeps_view(app, window, history, tasks)
        assert any(ends_above_foot), ends_above_foot


@LOCALES
def test_render_that_shows_the_horizontal_bar_keeps_the_row_whole(
    app, tmp_path, locale
):
    """A wider row from a render shrinks the view, so the current row moves up."""
    history = [_arrival("short", index) for index in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show(app, window, history, {})
        height, bar = listing.viewport().height(), listing.horizontalScrollBar()
        assert _enter_row(app, listing, 12).bottom() == height - 1
        assert not bar.isVisible()
        wide = [*history[:-1], _arrival("short", 29, "wide")]
        window.arrivals_screen.render(wide, {"wide": {"title": "W" * 600}})
        _process(app, 30)
        rect = listing.visualItemRect(listing.item(12))
        assert bar.isVisible()
        assert listing.viewport().height() < height
        assert listing.currentRow() == 12
        assert rect.bottom() == listing.viewport().height() - 1, rect


@LOCALES
def test_short_row_grown_by_render_stays_whole(app, tmp_path, locale):
    """A short row at the view's bottom that a render makes taller stays whole."""
    history = [_arrival("grow", i, "grow" if i == 12 else None) for i in range(30)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 1024, 700) as window,
    ):
        listing = _show(app, window, history, {"grow": {"title": "Arrival"}})
        before = _enter_row(app, listing, 12)
        height = listing.viewport().height()
        assert before.bottom() == height - 1
        grown = {"grow": {"title": "\n".join(["Arrival"] * 3)}}
        window.arrivals_screen.render(history, grown)
        _process(app, 30)
        rect = listing.visualItemRect(listing.item(12))
        assert listing.currentRow() == 12
        assert before.height() < rect.height() < height
        assert rect.bottom() == height - 1, rect


@LOCALES
@SIZES
@pytest.mark.parametrize("change", ["inserted", "removed"])
def test_row_changed_above_keeps_mid_row_reading(app, tmp_path, locale, size, change):
    """F2: a row inserted or removed above moved the row while the value stayed."""
    history = [_arrival("render", i, "tall" if i == 3 else None) for i in range(8)]
    tasks = {"tall": {"title": TALL_TITLE}}
    if change == "inserted":
        changed, row = [_arrival("new", 0), *history], 4
    else:
        changed, row = history[1:], 2
    with _scaled_font(app, 24.0), _tasks_window(app, tmp_path, locale, *size) as window:
        listing = _show(app, window, history, tasks)
        _read_into_middle(app, listing, 3)
        top = listing.visualItemRect(listing.item(3)).top()
        window.arrivals_screen.render(changed, tasks)
        _process(app, 30)
        assert listing.currentItem().data(Qt.ItemDataRole.UserRole) == "render-3"
        assert listing.currentRow() == row
        assert listing.visualItemRect(listing.item(row)).top() == top
        _press(app, Qt.Key.Key_Down)
        moved = top - listing.visualItemRect(listing.item(row)).top()
        assert listing.currentRow() == row
        assert 0 < moved <= _line_step(listing)


def _assert_steps_reverse(app, listing, forward, backward):
    """Walk the row with forward; after each step backward must undo it exactly."""
    bar = listing.verticalScrollBar()
    end = bar.maximum() if forward in DOWNWARD else bar.minimum()
    offsets = [bar.value()]
    for _ in range(bar.maximum() + 1):
        if offsets[-1] == end:
            return [after - before for before, after in pairwise(offsets)]
        _press(app, forward)
        offset = bar.value()
        _press(app, backward)
        assert bar.value() == offsets[-1], (forward, offsets, offset)
        _press(app, forward)
        offsets.append(offset)
    pytest.fail(f"{forward} never reached the end of the row: {offsets}")


@LOCALES
@pytest.mark.parametrize("page", [False, True], ids=["line", "page"])
def test_in_row_steps_reverse_exactly(app, tmp_path, locale, page):
    """F3: Down then Up, or PageDown then PageUp, returns to the same pixel."""
    down, up = (Qt.Key.Key_Down, Qt.Key.Key_Up)
    if page:
        down, up = (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp)
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        listing = _show_sized_tall_row(app, window, 1, 0)
        step = listing.viewport().height() if page else _line_step(listing)
        assert listing.verticalScrollBar().maximum() > step
        steps = _assert_steps_reverse(app, listing, down, up)
        # A page step is one whole viewport and a line step one text line,
        # except the last, which stops at the row's bottom.
        assert set(steps[:-1]) == {step} and 0 < steps[-1] <= step, steps
        steps = _assert_steps_reverse(app, listing, up, down)
        assert all(-step <= moved < 0 for moved in steps), steps


@LOCALES
@pytest.mark.parametrize("page", [False, True], ids=["line", "page"])
def test_steps_from_a_wheel_position_reverse_exactly(app, tmp_path, locale, page):
    """F3: Down then Up, and Up then Down, from a wheel position return to it."""
    down, up = (Qt.Key.Key_Down, Qt.Key.Key_Up)
    if page:
        down, up = (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp)
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        listing = _show_sized_tall_row(app, window, 1, 0, viewports=3.6)
        step = listing.viewport().height() if page else _line_step(listing)
        bar = listing.verticalScrollBar()
        # The sole row's offset is the scroll value. Half a step from either
        # edge, the step toward that edge is cut short; halfway between stops,
        # with a whole step to spare each way, neither step is.
        starts = (step // 2, step + step // 2, bar.maximum() - step // 2)
        assert 0 < step // 2 and bar.maximum() > starts[1] + step, (starts, step)
        for start in starts:
            for first, second, moved in ((down, up, step), (up, down, -step)):
                bar.setValue(start)
                _process(app)
                _press(app, first)
                stop = min(max(start + moved, 0), bar.maximum())
                assert bar.value() == stop, (first, start, bar.value())
                _press(app, second)
                assert bar.value() == start, (second, start)


def _cut_step_to_row_end(app, listing, last):
    """From half a line above the tall row's end, Down is cut short there."""
    bar = listing.verticalScrollBar()
    bar.setValue(last - _line_step(listing) // 2)
    _process(app)
    _press(app, Qt.Key.Key_Down)
    assert (listing.currentRow(), bar.value()) == (0, last)


@LOCALES
def test_only_the_next_reverse_key_retraces_a_cut_step(app, tmp_path, locale):
    """A page key, a wheel move, a retrace, or leaving the row makes steps whole."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        listing = _show_sized_tall_row(app, window, 2, 0, viewports=3.6)
        bar = listing.verticalScrollBar()
        line, page = _line_step(listing), listing.viewport().height()
        last = listing.visualItemRect(listing.item(0)).height() - page
        assert line < page and page + line < last, (line, page, last)
        _cut_step_to_row_end(app, listing, last)
        _press(app, Qt.Key.Key_PageUp)
        assert bar.value() == last - page
        _press(app, Qt.Key.Key_PageDown)
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - line
        _cut_step_to_row_end(app, listing, last)
        bar.setValue(last - page)
        _process(app)
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - page - line
        _cut_step_to_row_end(app, listing, last)
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - line // 2
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - line // 2 - line
        _cut_step_to_row_end(app, listing, last)
        _press(app, Qt.Key.Key_Down)
        _press(app, Qt.Key.Key_Up)
        assert (listing.currentRow(), bar.value()) == (0, last)
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - line


@LOCALES
def test_render_that_resizes_the_row_makes_steps_whole(app, tmp_path, locale):
    """A cut step is retraced only while its row keeps the size it was cut at."""
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        tasks = _sized_tasks(app, window, viewports=3.6)
        listing = _show(app, window, _sized_history(1, 0), tasks)
        bar = listing.verticalScrollBar()
        page = listing.viewport().height()
        last = listing.visualItemRect(listing.item(0)).height() - page
        _cut_step_to_row_end(app, listing, last)
        longer = {"tall": {"title": tasks["tall"]["title"] + "\nArrival وصول"}}
        window.arrivals_screen.render(_sized_history(1, 0), longer)
        _process(app, 30)
        assert (listing.currentRow(), bar.value()) == (0, last)
        _press(app, Qt.Key.Key_Up)
        assert bar.value() == last - _line_step(listing)


@LOCALES
def test_off_screen_tall_row_yields_to_row_navigation(app, tmp_path, locale):
    """A tall current row scrolled out of view moves to its neighbor, as Qt does."""
    history = [_arrival("away", i, "tall" if i in (1, 8) else None) for i in range(10)]
    with (
        _scaled_font(app, 18.0),
        _tasks_window(app, tmp_path, locale, 640, 360) as window,
    ):
        listing = _show(app, window, history, {"tall": {"title": TALL_TITLE}})
        bar = listing.verticalScrollBar()
        for key, current, value, row in (
            (Qt.Key.Key_Up, 1, bar.maximum(), 0),
            (Qt.Key.Key_Down, 8, 0, 9),
        ):
            listing.setCurrentRow(current)
            bar.setValue(value)
            _process(app)
            assert _band(listing, current) is None
            _press(app, key)
            assert listing.currentRow() == row, key
            assert _band(listing, row), key
