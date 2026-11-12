from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .arrival_minutes_wording import arrival_minutes
from .core import TERMINAL_STATUSES
from .localization import Strings
from .railway_palette import PLATFORM, PLATFORM_DEEP, PORCELAIN

_DOWNWARD_KEYS = (Qt.Key.Key_Down, Qt.Key.Key_PageDown)
_UPWARD_KEYS = (Qt.Key.Key_Up, Qt.Key.Key_PageUp)
_PAGE_KEYS = (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp)


@dataclass(frozen=True, slots=True)
class CutStep:
    """A step through a tall row that the row's edge cut short."""

    row: int
    rect: QRect
    step: int
    start: int


@dataclass(frozen=True, slots=True)
class ReadingAnchor:
    """Where the current row's top edge sat in the viewport, and at which sizes."""

    top: int
    line_spacing: int
    row_height: int
    view_height: int


@dataclass(frozen=True, slots=True)
class HeldAnchor:
    """A font change's reading anchor and the position applying it left."""

    anchor: ReadingAnchor
    position: tuple[int, int]


class ArrivalsList(QListWidget):
    """Arrival list whose keyboard paths read rows taller than the viewport.

    R54-D02: Qt row navigation jumps from one row edge to the next, so a
    focused keyboard user could never see the hidden part of a tall row.
    Up/Down scroll one text line and PageUp/PageDown one viewport through the
    current row before moving to its neighbor, and the reverse key retraces
    each step; Home/End reach the scroll limits at the boundary rows. Paging
    never skips content, and the current row keeps its reading position when
    the font or the rows above change, also while the window reflows for a
    new font.
    """

    def __init__(self) -> None:
        super().__init__()
        self._line_spacing = self.fontMetrics().lineSpacing()
        self._cut_step: CutStep | None = None
        self._held_anchor: HeldAnchor | None = None

    def doItemsLayout(self) -> None:
        # A new font reaches the widget before its rows are laid out again,
        # so their positions still match what the user was reading.
        spacing = self.fontMetrics().lineSpacing()
        if spacing != self._line_spacing:
            self._hold_reading_anchor()
        anchor = self._held_reading_anchor()
        super().doItemsLayout()
        self._line_spacing = spacing
        self._apply_held_anchor(anchor)

    def resizeEvent(self, event: QResizeEvent) -> None:
        # The window reflows after a font change and resizes the viewport.
        # The held anchor is checked before Qt clamps the scroll value to the
        # new range, then keeps the current row where the new font put it.
        anchor = self._held_reading_anchor()
        super().resizeEvent(event)
        self._apply_held_anchor(anchor)

    def _hold_reading_anchor(self) -> None:
        # An anchor still held from an earlier font change is kept, so a
        # font change and its reverse return the row to where it was.
        if self._held_reading_anchor() is not None:
            return
        anchor = self.reading_anchor()
        # Reading the anchor runs a pending layout, which may hold one first.
        if anchor is not None and self._held_anchor is None:
            self._held_anchor = HeldAnchor(anchor, self._position())

    def _held_reading_anchor(self) -> ReadingAnchor | None:
        # The anchor holds only while nothing else has moved the view or
        # changed the current row since it was applied.
        held = self._held_anchor
        if held is not None and held.position == self._position():
            return held.anchor
        self._held_anchor = None
        return None

    def _apply_held_anchor(self, anchor: ReadingAnchor | None) -> None:
        if anchor is None:
            return
        self.restore_reading_anchor(anchor)
        self._held_anchor = HeldAnchor(anchor, self._position())

    def reading_anchor(self) -> ReadingAnchor | None:
        """Return the current row's position, or None when it is off-screen."""
        item = self.currentItem()
        if item is None:
            return None
        rect = self.visualItemRect(item)
        if not rect.intersects(self.viewport().rect()):
            return None
        return ReadingAnchor(
            rect.top(), self._line_spacing, rect.height(), self.viewport().height()
        )

    def restore_reading_anchor(self, anchor: ReadingAnchor) -> bool:
        item = self.currentItem()
        if item is None:
            return False
        rect = self.visualItemRect(item)
        height = self.viewport().height()
        sizes = (self._line_spacing, rect.height(), height)
        if sizes == (anchor.line_spacing, anchor.row_height, anchor.view_height):
            # Nothing the reader sees changed size, so the view stays exactly
            # put, also where that leaves the current row partly hidden.
            return self._scroll_by(rect.top() - anchor.top)
        top = anchor.top
        if top < 0:
            # The text already read above the viewport scales with the font.
            top = -(-top * self._line_spacing // anchor.line_spacing)
        spare = height - rect.height()
        if spare >= 0:
            # A short row stays whole.
            top = max(0, min(top, spare))
        else:
            # A tall row keeps its top while that stays in view, as a smaller
            # viewport may not allow, and still fills the view to its bottom.
            top = max(spare, top if top < height else 0)
        return self._scroll_by(rect.top() - top)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if self._scroll_within_current_row(key):
            event.accept()
            return
        self._cut_step = None
        before = self._position()
        super().keyPressEvent(event)
        settled = self._settle_navigation(key, before[0])
        if self._page_stalled(key, before):
            settled = self._page_viewport(key, before)
        if settled:
            event.accept()

    def _scroll_within_current_row(self, key: int) -> bool:
        rect = self._tall_current_rect()
        if rect is None or key not in (*_DOWNWARD_KEYS, *_UPWARD_KEYS):
            return False
        height = self.viewport().height()
        # Steps never exceed the viewport, so consecutive views leave no gap.
        step = height if key in _PAGE_KEYS else min(self._line_spacing, height)
        offset, last = -rect.top(), rect.height() - height
        if key in _DOWNWARD_KEYS:
            if offset >= last:
                return False
            return self._step_within(rect, step, min(offset + step, last))
        if offset <= 0:
            return False
        return self._step_within(rect, step, max(offset - step, 0))

    def _step_within(self, rect: QRect, step: int, stop: int) -> bool:
        # Steps are relative, so a step and its reverse retrace each other,
        # even from a wheel position. A step that the row's edge cut short is
        # remembered, so the reverse step returns to where it began rather
        # than a whole step past it.
        row, offset, cut = self.currentRow(), -rect.top(), self._cut_step
        self._cut_step = None
        if cut is not None and (cut.row, cut.rect, cut.step) == (row, rect, step):
            stop = cut.start
        elif abs(stop - offset) < step:
            landed = rect.translated(0, offset - stop)
            self._cut_step = CutStep(row, landed, step, offset)
        return self._scroll_by(stop - offset)

    def _settle_navigation(self, key: int, previous_row: int) -> bool:
        row = self.currentRow()
        if row < 0:
            return False
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            return self._reach_scroll_limit(key, row)
        if key in _UPWARD_KEYS and row != previous_row:
            return self._show_current_row_bottom()
        return False

    def _page_stalled(self, key: int, before: tuple[int, int]) -> bool:
        # Qt paging can move neither the current row nor the view beside a
        # row taller than the viewport, and in some small lists of short rows.
        if key not in _PAGE_KEYS or self.count() == 0:
            return False
        return self._position() == before

    def _page_viewport(self, key: int, before: tuple[int, int]) -> bool:
        if not self._enter_edge_row(key):
            height = self.viewport().height()
            self._scroll_by(height if key == Qt.Key.Key_PageDown else -height)
            self._set_current_keeping_view(self._paged_row(key))
        return self._position() != before

    def _enter_edge_row(self, key: int) -> bool:
        # A tall row partly shown at the edge is entered at its near end, as
        # Down and Up enter it, so paging back through it retraces the steps.
        height = self.viewport().height()
        row = self._edge_row(key)
        rect = self.visualItemRect(self.item(row))
        # A viewport-tall row shows whole on one page, so it has no near end
        # to enter first and pages like a short row.
        if rect.height() <= height:
            return False
        if key == Qt.Key.Key_PageDown and rect.top() > 0:
            self._scroll_by(rect.top())
        elif key == Qt.Key.Key_PageUp and rect.bottom() < height - 1:
            self._scroll_by(rect.bottom() + 1 - height)
        else:
            return False
        self._set_current_keeping_view(row)
        return True

    def _edge_row(self, key: int) -> int:
        """Return the row at the viewport edge that paging moves toward."""
        viewport = self.viewport().rect()
        forward = key == Qt.Key.Key_PageDown
        y = viewport.bottom() if forward else viewport.top()
        index = self.indexAt(QPoint(viewport.center().x(), y))
        if index.isValid():
            return index.row()
        return self.count() - 1 if forward else 0

    def _paged_row(self, key: int) -> int:
        # The edge row of the new page becomes current. When the page cuts it,
        # its inner neighbor is shown whole instead by pulling the page back
        # toward the previous one, which skips nothing.
        forward = key == Qt.Key.Key_PageDown
        row = self._edge_row(key)
        inner = row - 1 if forward else row + 1
        if not self._cut_by_page(row, forward) or not 0 <= inner < self.count():
            return row
        height = self.viewport().height()
        rect = self.visualItemRect(self.item(inner))
        # A viewport-tall inner row still fits the page, so the page is pulled
        # back to show it whole. Only a taller one cannot be shown whole.
        if rect.height() > height:
            return row
        if forward:
            self._scroll_by(min(rect.top(), 0))
        else:
            self._scroll_by(max(rect.bottom() + 1 - height, 0))
        return inner

    def _cut_by_page(self, row: int, forward: bool) -> bool:
        rect = self.visualItemRect(self.item(row))
        height = self.viewport().height()
        # A viewport-tall row fits the page exactly, so like a short row it is
        # cut only when an edge lies outside the page.
        if rect.height() <= height:
            return rect.top() < 0 or rect.bottom() >= height
        # A tall row is entered only at its near end. When that end lies
        # inside the page, the next page enters the row there instead.
        return rect.top() > 0 if forward else rect.bottom() < height - 1

    def _set_current_keeping_view(self, row: int) -> None:
        # Qt scrolls a new current row to its own edge; keep the page where
        # paging put it so consecutive pages stay contiguous.
        bar = self.verticalScrollBar()
        value = bar.value()
        self.setCurrentRow(row)
        bar.setValue(value)

    def _reach_scroll_limit(self, key: int, row: int) -> bool:
        bar = self.verticalScrollBar()
        if key == Qt.Key.Key_End and row == self.count() - 1:
            return self._scroll_by(bar.maximum() - bar.value())
        if key == Qt.Key.Key_Home and row == 0:
            return self._scroll_by(bar.minimum() - bar.value())
        return False

    def _show_current_row_bottom(self) -> bool:
        # Entering a tall row from below starts at its bottom so upward
        # reading continues through it instead of skipping to its top.
        rect = self._tall_current_rect()
        if rect is None:
            return False
        return self._scroll_by(rect.bottom() - self.viewport().height() + 1)

    def _tall_current_rect(self) -> QRect | None:
        item = self.currentItem()
        if item is None:
            return None
        rect = self.visualItemRect(item)
        viewport = self.viewport().rect()
        # A row exactly as tall as the viewport counts as tall, so the keys
        # also read the part of it that a viewport edge cuts off.
        if rect.height() < viewport.height() or not rect.intersects(viewport):
            return None
        return rect

    def _position(self) -> tuple[int, int]:
        return self.currentRow(), self.verticalScrollBar().value()

    def _scroll_by(self, delta: int) -> bool:
        bar = self.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + delta)
        return bar.value() != before


class ArrivalsScreen(QFrame):
    def __init__(self, strings: Strings, device_id: str) -> None:
        super().__init__()
        self._render_signature: tuple[Any, ...] | None = None
        self.strings = strings
        self.setObjectName("ticket")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        self._build_body(layout, device_id)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.strings.text("arrivals.title"))
        title.setObjectName("sectionTitle")
        self.history_scope = QLabel(self.strings.text("arrivals.scope"))
        self.history_scope.setObjectName("taskSubtitle")
        self.history_scope.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(self.history_scope)
        self.history_count = QLabel("0")
        self.history_count.setObjectName("countBadge")
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(self.history_count)
        return header

    def _build_body(self, layout: QVBoxLayout, device_id: str) -> None:
        self.history_list = ArrivalsList()
        self.history_list.setAccessibleName(self.strings.text("arrivals.accessible"))
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history_list.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.history_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.history_list, 1)
        self.device_label = QLabel(
            self.strings.text("device.label", device=device_id[-8:].upper())
        )
        self.device_label.setObjectName("device")
        layout.addWidget(self.device_label)

    def render(
        self,
        history: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
    ) -> None:
        retained = [item for item in history if item.get("status") in TERMINAL_STATUSES]
        signature = self._signature(retained, known_tasks)
        if signature == self._render_signature:
            return
        self._render_signature = signature
        self._render_header(retained)
        current_id = self._current_arrival_id()
        scroll = self._scroll_position()
        anchor = self.history_list.reading_anchor()
        self.history_list.clear()
        # Med: show every retained arrival; the list scrolls instead of
        # hard-truncating at 8 rows.
        for item in retained:
            self._render_item(item, known_tasks)
        if not retained:
            self._render_empty()
        self._restore_arrival(current_id, scroll, anchor)

    def _signature(
        self,
        retained: list[dict[str, Any]],
        known_tasks: dict[str, dict[str, Any]],
    ) -> tuple[Any, ...]:
        return tuple(
            (
                item.get("id"),
                item.get("timerId"),
                item.get("taskId"),
                item.get("phase"),
                item.get("status"),
                item.get("plannedDurationMs"),
                item.get("elapsedMs"),
                item.get("completedAt") or item.get("endedAt"),
                item.get("pending"),
                self._task_label(item.get("taskId"), known_tasks),
            )
            for item in retained
        )

    def _current_arrival_id(self) -> Any:
        current = self.history_list.currentItem()
        if current is None:
            return None
        return current.data(Qt.ItemDataRole.UserRole)

    def _scroll_position(self) -> tuple[int, int]:
        return (
            self.history_list.verticalScrollBar().value(),
            self.history_list.horizontalScrollBar().value(),
        )

    def _restore_arrival(
        self,
        current_id: Any,
        scroll: tuple[int, int],
        anchor: ReadingAnchor | None,
    ) -> None:
        restored = self._select_arrival(current_id)
        self.history_list.verticalScrollBar().setValue(scroll[0])
        self.history_list.horizontalScrollBar().setValue(scroll[1])
        # Rows inserted, removed, or resized above the current row must not
        # move it out of view mid-read.
        if restored and anchor is not None:
            self.history_list.restore_reading_anchor(anchor)

    def _select_arrival(self, arrival_id: Any) -> bool:
        if arrival_id is None:
            return False
        for row in range(self.history_list.count()):
            item = self.history_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == arrival_id:
                self.history_list.setCurrentRow(row)
                return True
        return False

    def _render_header(self, retained: list[dict[str, Any]]) -> None:
        displayed = len(retained)
        self.history_count.setText(
            self.strings.text(
                "arrivals.count", displayed=displayed, total=len(retained)
            )
        )
        self.history_list.setAccessibleDescription(
            self.strings.text(
                "arrivals.description", displayed=displayed, total=len(retained)
            )
        )

    def _render_item(
        self,
        item: dict[str, Any],
        known_tasks: dict[str, dict[str, Any]],
    ) -> None:
        phase_value = str(item.get("phase", "timer"))
        phase = self._phase_label(phase_value)
        task_label = self._task_label(item.get("taskId"), known_tasks)
        wording = arrival_minutes(item)
        when = item.get("completedAt") or item.get("endedAt")
        row_key = (
            "arrivals.row_planned" if wording.reads_as_planned else "arrivals.row"
        )
        text = self.strings.text(
            row_key,
            phase=phase,
            status=self.strings.text(wording.status_key),
            task=task_label,
            minutes=wording.minutes,
            when=self._time_label(when),
            pending=self.strings.text("arrivals.pending")
            if item.get("pending")
            else "",
        )
        row = QListWidgetItem(text, self.history_list)
        row.setData(Qt.ItemDataRole.UserRole, item.get("id"))
        row.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))

    def _phase_label(self, phase: str) -> str:
        key = f"phase.{phase}"
        return self.strings.text(key) if key in self.strings.messages else phase

    def _task_label(
        self,
        task_id: str | None,
        known_tasks: dict[str, dict[str, Any]],
    ) -> str:
        task = known_tasks.get(task_id) if task_id else None
        if task:
            return task["title"]
        if task_id:
            return self.strings.text("task.deleted")
        return self.strings.text("task.unassigned")

    def _time_label(self, when: object) -> str:
        try:
            return (
                datetime.fromisoformat(
                    str(when).replace("Z", "+00:00")  # noqa: FURB162
                )
                .astimezone()
                .strftime("%a %H:%M")
            )
        except (ValueError, TypeError):
            return self.strings.text("arrivals.time_pending")

    def _render_empty(self) -> None:
        text = self.strings.text("arrivals.empty")
        item = QListWidgetItem(text, self.history_list)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def stylesheet() -> str:
        return """
        QLabel#countBadge { background: @platform; color: @porcelain; border: 2px solid @platform_deep; padding: 2px 8px; font-weight: bold; }
        QListWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); outline: none; padding: 3px; }
        QListWidget::item { border-bottom: 1px solid palette(alternate-base); padding: 7px 5px; }
        """.replace("@platform_deep", PLATFORM_DEEP).replace("@platform", PLATFORM).replace("@porcelain", PORCELAIN)
