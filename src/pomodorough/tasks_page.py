from __future__ import annotations

from PySide6.QtCore import QEvent, QMargins, QObject, QTimer
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

from .tasks_screen import TasksScreen

# Docking adds a viewport margin, which can add a scroll bar and narrow the
# banner once. The second pass settles on the narrowed width.
_PLACEMENT_PASSES = 2


class TasksPage(QScrollArea):
    """Scrolls the Tasks screen and docks its undo banner under the viewport.

    A docked banner stays visible while the task rows scroll, and it never
    moves a row: the space comes from a viewport margin, not from the screen
    layout. The margin does not change the size hints of the page.

    Opening the page, and focus entering the add form, scroll the form into
    view: large fonts in a small window can push it under the viewport.
    """

    def __init__(self, screen: TasksScreen) -> None:
        super().__init__()
        self.screen = screen
        self._placing = False
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setWidget(screen)
        screen.undo_window_changed.connect(self.place_undo_banner)
        screen.undo_banner.installEventFilter(self)
        for widget in self._form_widgets():
            widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # A new title or font changes the height the banner needs.
        if (
            watched is self.screen.undo_banner
            and event.type() == QEvent.Type.LayoutRequest
        ):
            self.place_undo_banner()
        if watched in self._form_widgets() and event.type() == QEvent.Type.FocusIn:
            self.reveal_add_form()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.place_undo_banner()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # The screen settles its header text and rows after it is shown, so
        # reveal the form once that layout pass has run.
        QTimer.singleShot(0, self, self.reveal_add_form)

    def _form_widgets(self) -> tuple[QWidget, ...]:
        return (self.screen.task_input, self.screen.add_task_button)

    def reveal_add_form(self) -> None:
        for widget in self._form_widgets():
            self.ensureWidgetVisible(widget, 0, 0)

    def undo_banner_docked(self) -> bool:
        return self.screen.undo_banner.parentWidget() is self

    def place_undo_banner(self) -> None:
        if self._placing:
            return
        self._placing = True
        try:
            for _ in range(_PLACEMENT_PASSES):
                self._place_undo_banner_once()
        finally:
            self._placing = False
        self.screen.link_undo_tab_order()

    def _place_undo_banner_once(self) -> None:
        if self.screen.pending_delete_task_id() is None:
            self._release_undo_banner()
            return
        banner = self.screen.undo_banner
        width = self.viewport().width()
        banner.fit_width(width)
        height = banner.height_for(width)
        if self._dock_fits(width, height):
            self._dock_undo_banner(height)
        else:
            self._release_undo_banner()

    def _dock_fits(self, width: int, height: int) -> bool:
        # The rows keep at least one whole row above a docked banner. When
        # that is not possible (large fonts in a small window), the banner
        # stays in the page flow under the table and scrolls with it.
        banner = self.screen.undo_banner
        page_height = self.viewport().height() + self.viewportMargins().bottom()
        return (
            banner.minimumSizeHint().width() <= width
            and page_height - height >= self.screen.tallest_row_height()
        )

    def _dock_undo_banner(self, height: int) -> None:
        banner = self.screen.undo_banner
        held_focus = banner.holds_focus()
        if banner.parentWidget() is not self:
            banner.setParent(self)
        self.setViewportMargins(QMargins(0, 0, 0, height))
        viewport = self.viewport().geometry()
        banner.setGeometry(viewport.x(), viewport.bottom() + 1, viewport.width(), height)
        banner.show()
        banner.raise_()
        if held_focus:
            banner.undo_button.setFocus()

    def _release_undo_banner(self) -> None:
        self.setViewportMargins(QMargins(0, 0, 0, 0))
        self.screen.rest_undo_banner(self.viewport().width())
