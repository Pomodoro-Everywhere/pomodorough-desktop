"""Header navigation between the four screens.

The destinations sit in one row when the header has room for it. When it
does not, they collapse into one button that names the current screen and
opens a menu of all four (UX round 2 B1). The navigation never stacks, so
the header keeps its one-row height at every font and window size.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QWidget,
)

DESTINATIONS = ("timer", "tasks", "arrivals", "network")


class ScreenNavigation(QWidget):
    screen_requested = Signal(int)

    def __init__(self, strings: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._strings = strings
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        self._nav_layout = QGridLayout(self)
        self._nav_layout.setContentsMargins(0, 0, 0, 0)
        self._nav_mode = "row"
        self._selecting = False
        self._pressed_page: int | None = None
        self._current = 0
        self._menu_label_width: int | None = None
        for index, destination in enumerate(DESTINATIONS):
            button = self._button(destination)
            self._connect_button(button, index)
            self.group.addButton(button)
            self.buttons.append(button)
            self._nav_layout.addWidget(button, 0, index)
        self._build_destination_menu()
        self.select(0)

    def _connect_button(self, button: QPushButton, index: int) -> None:
        # Screen readers (VoiceOver, UIA, AT-SPI) drive checkable buttons
        # through QAbstractButton.toggle(), which never emits clicked, so
        # a toggle that no press started requests the screen. A pointer
        # or keyboard click runs pressed, toggled, released, clicked;
        # clicked requests the screen even when the tab is already
        # current, so re-clicking the active tab still refreshes it.
        button.pressed.connect(lambda page=index: self._press_started(page))
        button.released.connect(self._press_ended)
        button.toggled.connect(
            lambda checked, page=index: self._screen_toggled(page, checked)
        )
        button.clicked.connect(
            lambda checked=False, page=index: self.screen_requested.emit(page)
        )

    def _build_destination_menu(self) -> None:
        self.menu_button = QPushButton()
        self.menu_button.setObjectName("navigationMenuButton")
        self.menu_button.setProperty("screen", True)
        self.destination_menu = QMenu(self.menu_button)
        self.destination_actions: list[QAction] = []
        actions = QActionGroup(self)
        actions.setExclusive(True)
        for index, button in enumerate(self.buttons):
            action = self.destination_menu.addAction(button.text())
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, page=index: self.screen_requested.emit(page)
            )
            actions.addAction(action)
            self.destination_actions.append(action)
        self.menu_button.setMenu(self.destination_menu)
        self.menu_button.hide()
        self._nav_layout.addWidget(self.menu_button, 0, len(self.buttons))

    def select(self, index: int) -> None:
        """Mark a screen as current without requesting navigation to it."""
        self._selecting = True
        try:
            self.buttons[index].setChecked(True)
        finally:
            self._selecting = False
        self._current = index
        self.destination_actions[index].setChecked(True)
        destination = self._strings.text(f"destination.{DESTINATIONS[index]}")
        self.menu_button.setAccessibleName(
            self._strings.text("nav.menu_accessible", destination=destination)
        )
        self._show_menu_label(self._menu_label_width)

    def _press_started(self, page: int) -> None:
        self._pressed_page = page

    def _press_ended(self) -> None:
        self._pressed_page = None

    def _screen_toggled(self, page: int, checked: bool) -> None:
        if not checked or self._selecting or self._pressed_page == page:
            return
        self.screen_requested.emit(page)

    def set_nav_mode(self, mode: str) -> None:
        """Show the destinations as a row, or collapse them into the menu."""
        if mode == self._nav_mode:
            return
        self._nav_mode = mode
        collapsed = mode == "menu"
        if collapsed and any(button.hasFocus() for button in self.buttons):
            # The focused destination is about to hide; keep keyboard focus
            # on the navigation instead of losing it to the window.
            self.menu_button.setFocus(Qt.FocusReason.OtherFocusReason)
        for button in self.buttons:
            button.setVisible(not collapsed)
        self.menu_button.setVisible(collapsed)
        if not collapsed and self.menu_button.hasFocus():
            self.buttons[self._current].setFocus(Qt.FocusReason.OtherFocusReason)
        self.updateGeometry()

    def measured_nav_needs(self) -> tuple[int, int]:
        """Widths the one-row navigation and the whole menu button need."""
        hints = [button.minimumSizeHint().width() for button in self.buttons]
        margins = self._nav_layout.contentsMargins()
        chrome = margins.left() + margins.right()
        gap = self._nav_gap()
        need_row = sum(hints) + gap * (len(hints) - 1) + chrome
        return need_row, self._menu_chrome_width() + self._widest_label() + chrome

    def fit_menu_label(self, width: int) -> None:
        """Shorten the menu button label when the header has less room."""
        self._show_menu_label(width)

    def _show_menu_label(self, width: int | None) -> None:
        self._menu_label_width = width
        label = self.buttons[self._current].text()
        shown = label
        if width is not None:
            room = max(0, width - self._menu_chrome_width())
            metrics = self.menu_button.fontMetrics()
            shown = metrics.elidedText(label, Qt.TextElideMode.ElideRight, room)
        if self.menu_button.text() != shown:
            self.menu_button.setText(shown)
        # An elided label keeps the full destination name one hover away.
        self.menu_button.setToolTip(label if shown != label else "")

    def _menu_chrome_width(self) -> int:
        """Menu button width spent on padding, border and the menu arrow."""
        text = self.menu_button.text()
        label_width = self.menu_button.fontMetrics().horizontalAdvance(text)
        return max(0, self.menu_button.minimumSizeHint().width() - label_width)

    def _widest_label(self) -> int:
        metrics = self.menu_button.fontMetrics()
        return max(metrics.horizontalAdvance(button.text()) for button in self.buttons)

    def _nav_gap(self) -> int:
        raw = self._nav_layout.horizontalSpacing()
        if raw >= 0:
            return raw
        # DSK-01: the macOS style has no single layout spacing, so the
        # grid spaces its cells by control type. Ask the style the same
        # question; a zero gap here undermeasured the row and clipped it.
        push = QSizePolicy.ControlType.PushButton
        try:
            gap = self.style().combinedLayoutSpacing(
                push, push, Qt.Orientation.Horizontal, None, self
            )
        except RuntimeError:
            return 0
        return max(0, gap)

    def _button(self, destination: str) -> QPushButton:
        button = QPushButton(self._strings.text(f"nav.{destination}"))
        button.setAccessibleName(
            self._strings.text(
                "nav.show",
                destination=self._strings.text(f"destination.{destination}"),
            )
        )
        button.setCheckable(True)
        button.setProperty("screen", True)
        return button
