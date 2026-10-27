from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .arrivals_screen import ArrivalsScreen
from .core import format_remaining
from .network_screen import PRIVACY_POLICY_URL as PRIVACY_POLICY_URL, NetworkScreen
from .secure_store import SecureStoreError, should_capture_secure_store_error
from .sentry_monitoring import (
    capture_exception,
    note_clock_recovery,
    note_room_read_recovery,
    should_capture_clock_failure,
    should_capture_room_read_failure,
)
from .shared_core import SharedCoreError
from .tasks_screen import TasksScreen
from .timer_screen import TimerRenderState, TimerScreen

_BASE_APP_PT = 9.0


def _app_font_scale() -> float:
    try:
        app = QApplication.instance()
        if app is not None and app.font().pointSizeF() > 0:
            return float(app.font().pointSizeF()) / _BASE_APP_PT
    except RuntimeError:
        pass
    return 1.0


class ScreenNavigation(QWidget):
    screen_requested = Signal(int)

    # R52-D05: measured reflow cells. Buttons keep identity, group,
    # and signals; only grid cells change, so focus and routing
    # survive. QGridLayout mirrors columns under RTL by itself.
    _NAV_CELLS = {
        "row": ((0, 0), (0, 1), (0, 2), (0, 3)),
        "grid": ((0, 0), (0, 1), (1, 0), (1, 1)),
        "column": ((0, 0), (1, 0), (2, 0), (3, 0)),
    }

    def __init__(self, strings: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        self._nav_layout = QGridLayout(self)
        self._nav_layout.setContentsMargins(0, 0, 0, 0)
        self._nav_mode = "row"
        destinations = (
            ("timer", strings.text("nav.timer")),
            ("tasks", strings.text("nav.tasks")),
            ("arrivals", strings.text("nav.arrivals")),
            ("network", strings.text("nav.network")),
        )
        for index, (destination, label) in enumerate(destinations):
            button = self._button(strings, destination, label)
            button.clicked.connect(
                lambda checked=False, page=index: self.screen_requested.emit(page)
            )
            self.group.addButton(button)
            self.buttons.append(button)
        self._place_nav_buttons()
        self.buttons[0].setChecked(True)

    def _place_nav_buttons(self) -> None:
        for button, cell in zip(self.buttons, self._NAV_CELLS[self._nav_mode]):
            self._nav_layout.addWidget(button, *cell)

    def set_nav_mode(self, mode: str) -> None:
        if mode == self._nav_mode:
            return
        self._nav_mode = mode
        self._place_nav_buttons()
        self.updateGeometry()

    def measured_nav_needs(self) -> tuple[int, int, int]:
        hints = [button.minimumSizeHint().width() for button in self.buttons]
        margins = self._nav_layout.contentsMargins()
        chrome = margins.left() + margins.right()
        gap = self._nav_gap(horizontal=True)
        need_row = sum(hints) + gap * (len(hints) - 1) + chrome
        need_grid = max(hints[0], hints[2]) + max(hints[1], hints[3]) + gap + chrome
        return need_row, need_grid, max(hints) + chrome

    def _nav_gap(self, *, horizontal: bool) -> int:
        raw = (
            self._nav_layout.horizontalSpacing()
            if horizontal
            else self._nav_layout.verticalSpacing()
        )
        if raw >= 0:
            return raw
        try:
            metric = (
                QStyle.PixelMetric.PM_LayoutHorizontalSpacing
                if horizontal
                else QStyle.PixelMetric.PM_LayoutVerticalSpacing
            )
            return max(0, self.style().pixelMetric(metric))
        except RuntimeError:
            return 0

    @staticmethod
    def _button(strings: Any, destination: str, label: str) -> QPushButton:
        button = QPushButton(label)
        button.setAccessibleName(
            strings.text(
                "nav.show",
                destination=strings.text(f"destination.{destination}"),
            )
        )
        button.setCheckable(True)
        button.setProperty("screen", True)
        return button


class MainWindowViewMixin:
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        self.outer_layout = QVBoxLayout(root)
        self._build_header()
        self._build_rule()
        self._build_pages()
        self._connect_screen_signals()
        self._build_shortcuts()
        self.notice.connect(self._show_notice)
        self._refresh_stylesheet()
        self._apply_responsive_layout()

    def _build_header(self) -> None:
        header = QHBoxLayout()
        self._header_layout = header
        self.navigation = ScreenNavigation(self.strings, self)
        self.screen_group = self.navigation.group
        self.screen_buttons = self.navigation.buttons
        # R52-D05: the nav group scrolls instead of overlapping when the
        # measured stack still exceeds the header (short window, huge
        # font). No frame, bars only on overflow; fitting modes are
        # visually unchanged.
        self._nav_scroll = QScrollArea()
        self._nav_scroll.setObjectName("navScroll")
        self._nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._nav_scroll.setWidgetResizable(True)
        # R52-D05: QScrollArea reports a fixed 88x88 minimum of its own.
        # Height follows the header (same as the bare nav widget before);
        # the viewport scrolls when the measured stack needs more.
        self._nav_scroll.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        self._nav_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._nav_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._nav_scroll.setWidget(self.navigation)
        header.addWidget(self._nav_scroll)
        header.addStretch()
        self._build_header_actions(header)
        self.outer_layout.addLayout(header)

    def _build_header_actions(self, header: QHBoxLayout) -> None:
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setCheckable(True)
        self.settings_button.setMinimumSize(40, 40)
        self.settings_button.setIconSize(QSize(28, 28))
        icon = QIcon.fromTheme("settings-configure")
        if icon.isNull():
            self.settings_button.setText("⚙")
            self.settings_button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextOnly
            )
        else:
            self.settings_button.setIcon(icon)
            self.settings_button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly
            )
        label = self.strings.text("status.settings_show")
        self.settings_button.setToolTip(label)
        self.settings_button.setAccessibleName(label)
        self.settings_button.toggled.connect(self._settings_toggled)
        self.account_button = QPushButton(self.strings.text("account.sign_in"))
        self.account_button.setObjectName("accountButton")
        self.account_button.clicked.connect(self._account_action)
        header.addWidget(self.settings_button)
        header.addWidget(self.account_button)

    def _build_rule(self) -> None:
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setObjectName("rule")
        self.outer_layout.addWidget(rule)

    def _build_pages(self) -> None:
        self.page_stack = QStackedWidget()
        self.timer_screen = TimerScreen(self.strings, self.settings)
        self.tasks_screen = TasksScreen(self.strings)
        self.tasks_page = QScrollArea()
        self.tasks_page.setFrameShape(QFrame.Shape.NoFrame)
        self.tasks_page.setWidgetResizable(True)
        self.tasks_page.setWidget(self.tasks_screen)
        self.arrivals_screen = ArrivalsScreen(self.strings, self.store.device_id)
        self.network_screen = NetworkScreen(self.strings, self.replication_mode)
        for screen in self._screens():
            self.page_stack.addWidget(screen)
        self.outer_layout.addWidget(self.page_stack, 1)
        self._expose_timer_seams()
        self._expose_tasks_seams()
        self._expose_arrivals_seams()
        self._expose_network_seams()

    def _screens(self) -> tuple[QWidget, ...]:
        return (
            self.timer_screen,
            self.tasks_page,
            self.arrivals_screen,
            self.network_screen,
        )

    def _expose_timer_seams(self) -> None:
        self.timer_page = self.timer_screen
        names = (
            "content_layout",
            "left_layout",
            "clock",
            "active_task_context",
            "actions_layout",
            "task_selector_panel",
            "task_combo",
            "primary_button",
            "finish_button",
            "cancel_button",
            "stop_sound_button",
            "right_panel",
            "settings_scroll",
            "right_layout",
            "pattern_scope",
            "phase_group",
            "phase_buttons",
            "duration_spins",
            "auto_breaks",
            "alert_guarantee",
        )
        for name in names:
            setattr(self, name, getattr(self.timer_screen, name))

    def _expose_tasks_seams(self) -> None:
        for name in (
            "task_totals",
            "task_input",
            "add_task_button",
            "task_table",
            "tasks_empty",
        ):
            setattr(self, name, getattr(self.tasks_screen, name))

    def _expose_arrivals_seams(self) -> None:
        self.arrivals_page = self.arrivals_screen
        for name in (
            "history_scope",
            "history_count",
            "history_list",
            "device_label",
        ):
            setattr(self, name, getattr(self.arrivals_screen, name))

    def _expose_network_seams(self) -> None:
        self.network_page = self.network_screen
        names = (
            "network_status",
            "replication_mode_combo",
            "privacy_policy_button",
            "delete_account_button",
            "network_unavailable",
            "iroh_first_room_guidance",
            "iroh_panel",
            "room_name_input",
            "create_room_button",
            "invite_input",
            "join_room_button",
            "invite_output",
            "copy_invite_button",
            "refresh_invite_button",
            "sync_iroh_button",
            "leave_room_button",
            "network_details",
        )
        for name in names:
            setattr(self, name, getattr(self.network_screen, name))

    def _connect_screen_signals(self) -> None:
        self.navigation.screen_requested.connect(self._show_screen)
        timer = self.timer_screen
        timer.primary_action_requested.connect(self._primary_action)
        timer.command_requested.connect(self._issue)
        timer.stop_sound_requested.connect(self._stop_sound)
        timer.phase_selected.connect(self._select_phase)
        timer.duration_changed.connect(self._duration_changed)
        timer.task_selection_changed.connect(self._task_selection_changed)
        timer.auto_breaks_changed.connect(self._auto_breaks_changed)
        tasks = self.tasks_screen
        tasks.add_task_requested.connect(self._add_task)
        tasks.delete_task_requested.connect(self._delete_task)
        self._connect_network_signals()

    def _connect_network_signals(self) -> None:
        screen = self.network_screen
        screen.replication_mode_requested.connect(self._replication_mode_changed)
        screen.privacy_policy_requested.connect(self._open_privacy_policy)
        screen.delete_account_requested.connect(self._delete_account_action)
        screen.create_room_requested.connect(self._create_iroh_room)
        screen.join_room_requested.connect(self._join_iroh_room)
        screen.copy_invite_requested.connect(self._copy_iroh_invite)
        screen.refresh_invite_requested.connect(self._refresh_iroh_invite)
        screen.sync_now_requested.connect(self._sync_iroh_now)
        screen.leave_room_requested.connect(self._leave_iroh_room)

    def _build_shortcuts(self) -> None:
        self.shortcuts = [
            QShortcut(QKeySequence(Qt.Key.Key_Space), self),
            QShortcut(QKeySequence("Ctrl+Shift+F"), self),
            *(QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self) for index in range(4)),
        ]
        self.shortcuts[0].setAutoRepeat(False)
        QApplication.instance().focusChanged.connect(self._update_space_shortcut)
        self._update_space_shortcut()
        self.shortcuts[0].activated.connect(self._shortcut_primary_action)
        self.shortcuts[1].activated.connect(lambda: self._issue("finish"))
        for index, shortcut in enumerate(self.shortcuts[2:]):
            shortcut.activated.connect(
                lambda page=index: self._shortcut_show_screen(page)
            )

    def _update_space_shortcut(self) -> None:
        # Shortcut activation is too late to return Space to native controls.
        # Update arbitration before the next key event, including focus loss.
        self.shortcuts[0].setEnabled(not self._focused_widget_consumes_space())

    @staticmethod
    def _focused_widget_consumes_space() -> bool:
        # D71: the global Space shortcut must not steal Space from the
        # focused control. Buttons/checkboxes click with Space, text inputs
        # type it, combos/spins consume it for popup/editing.
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        return isinstance(
            widget,
            (
                QAbstractButton,
                QLineEdit,
                QPlainTextEdit,
                QComboBox,
                QSpinBox,
            ),
        )

    @staticmethod
    def _focused_widget_is_text_input() -> bool:
        # D75: Ctrl+digit navigation must not fire while the user edits
        # text. QSpinBox/QComboBox own an embedded QLineEdit; when either
        # has focus the user is editing, so skip navigation.
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        return isinstance(
            widget, (QLineEdit, QPlainTextEdit, QSpinBox, QComboBox)
        )

    def _shortcut_primary_action(self) -> None:
        if self._focused_widget_consumes_space():
            return
        self._primary_action()

    def _shortcut_show_screen(self, index: int) -> None:
        if self._focused_widget_is_text_input():
            return
        self.navigation.screen_requested.emit(index)

    def _settings_toggled(self, visible: bool) -> None:
        self.timer_screen.set_settings_visible(visible)
        # D76: settings panel needs room beside the timer column; derive
        # the window minimum from layout size hints, capped to the
        # available screen width so small displays never clip. Scroll
        # (settings_scroll) covers short heights.
        if hasattr(self, "setMinimumWidth"):
            try:
                self.setMinimumWidth(self._settings_minimum_width(visible))
                self.updateGeometry()
            except RuntimeError:
                pass
        label = self.strings.text(
            "status.settings_hide" if visible else "status.settings_show"
        )
        self.settings_button.setToolTip(label)
        self.settings_button.setAccessibleName(label)

    def _settings_minimum_width(self, visible: bool) -> int:
        base = 600
        if not visible:
            return base
        hint = self._settings_content_width_hint()
        available = self._available_screen_width()
        if available > 0:
            return min(max(base, hint), available)
        return max(base, hint)

    def _settings_content_width_hint(self) -> int:
        try:
            hint = self.timer_screen.sizeHint().width()
        except RuntimeError:
            return 600
        margins = self.outer_layout.contentsMargins()
        chrome = margins.left() + margins.right() + 48
        return hint + chrome

    def _available_screen_width(self) -> int:
        try:
            screen = QApplication.primaryScreen()
        except RuntimeError:
            return 0
        if screen is None:
            return 0
        try:
            return screen.availableGeometry().width()
        except RuntimeError:
            return 0

    def _display_screen(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        self.screen_buttons[index].setChecked(True)
        self.settings_button.setVisible(index == 0)
        self._render()
        focus_targets = (
            self.primary_button,
            self.task_input,
            self.history_list,
            # D72: network screen needs a keyboard target like the other
            # three pages; the replication-mode combo is always focusable.
            self.replication_mode_combo,
        )
        if index < len(focus_targets):
            focus_targets[index].setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _apply_responsive_layout(self) -> None:
        landscape = self.width() > self.height()
        compact = self.width() < 780 or self.height() < 540
        if landscape == self._landscape and compact == self._compact:
            return
        self._landscape = landscape
        self._compact = compact
        self.outer_layout.setContentsMargins(
            12 if compact else 24,
            10 if compact else 18,
            12 if compact else 24,
            12 if compact else 22,
        )
        self.outer_layout.setSpacing(8 if compact else 15)
        self.timer_screen.apply_responsive_layout(
            landscape=landscape,
            compact=compact,
        )

    def _schedule_nav_reflow(self) -> None:
        # R52-D05: resize/change events run inside Qt event propagation,
        # where synchronous layout mutation aborts on some Qt releases.
        # Coalesce through the event loop like the settings reflow timer.
        timer = getattr(self, "_nav_reflow_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._reflow_navigation)
            self._nav_reflow_timer = timer
        if not timer.isActive():
            timer.start()

    def _reflow_navigation(self) -> None:
        navigation = getattr(self, "navigation", None)
        if navigation is None:
            return
        need_row, need_grid, _need_column = navigation.measured_nav_needs()
        avail = self._header_nav_avail()
        if avail <= 0:
            return
        if avail >= need_row:
            navigation.set_nav_mode("row")
        elif avail >= need_grid:
            navigation.set_nav_mode("grid")
        else:
            navigation.set_nav_mode("column")
        self._sync_nav_scrollbars(navigation)

    def _sync_nav_scrollbars(self, navigation: ScreenNavigation) -> None:
        # R52-D05: the row fit is exact, so bars stay off and the base
        # header keeps its height. Stacked modes scroll on demand for
        # short windows or extreme fonts.
        mode = navigation._nav_mode
        scroll = getattr(self, "_nav_scroll", None)
        if scroll is None:
            return
        stacked = mode != "row"
        horizontal = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if stacked
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        vertical = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if stacked
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if scroll.horizontalScrollBarPolicy() != horizontal:
            scroll.setHorizontalScrollBarPolicy(horizontal)
        if scroll.verticalScrollBarPolicy() != vertical:
            scroll.setVerticalScrollBarPolicy(vertical)
        size_policy = scroll.sizePolicy()
        vertical_policy = (
            QSizePolicy.Policy.Preferred
            if stacked
            else QSizePolicy.Policy.Ignored
        )
        if size_policy.verticalPolicy() != vertical_policy:
            size_policy.setVerticalPolicy(vertical_policy)
            scroll.setSizePolicy(size_policy)

    def _header_nav_avail(self) -> int:
        central = self.centralWidget()
        if central is None or central.width() <= 0:
            return 0
        margins = self.outer_layout.contentsMargins()
        avail = central.width() - margins.left() - margins.right()
        # Header holds nav scroll, stretch, settings, account: 3 gaps.
        avail -= 3 * self._header_gap()
        avail -= self._action_need(getattr(self, "settings_button", None))
        avail -= self._action_need(getattr(self, "account_button", None))
        return avail

    @staticmethod
    def _action_need(action: QWidget | None) -> int:
        if action is None or not action.isVisible():
            return 0
        try:
            return max(action.width(), action.minimumSizeHint().width())
        except RuntimeError:
            return 0

    def _header_gap(self) -> int:
        raw = self._header_layout.spacing()
        if raw >= 0:
            return raw
        try:
            metric = QStyle.PixelMetric.PM_LayoutHorizontalSpacing
            return max(0, self.style().pixelMetric(metric))
        except RuntimeError:
            return 0

    def _render(self) -> None:
        state = self._render_timer_state()
        self.timer_screen.render_clock(state, self.history)
        self._render_network()
        self._render_task_selector(state.display_timer, state.active)
        self._update_tray_progress(state.elapsed / state.planned, state.active)
        self.timer_screen.render_controls(
            state,
            selected_phase=self._selected_phase(),
            mutations_enabled=not self._history_resolution_active,
            sound_active=self._sound_active,
        )
        labels = self.timer_screen.status_labels()
        self._render_tray(state.status, state.remaining, labels)
        self._render_history()
        self.tasks_screen.render(
            self.tasks,
            self.history,
            mutations_enabled=not self._history_resolution_active,
        )
        self._render_completion_notification(state.source_timer, state.status)

    def _render_timer_state(self) -> TimerRenderState:
        source_timer = self._current_timer()
        now_ms = self._guarded_timer_now_ms(source_timer)
        return self.timer_screen.presentation(
            source_timer,
            selected_phase=self._selected_phase(),
            settings=self.settings,
            now_ms=now_ms,
            preview_phase=self._preview_completed_phase(source_timer, now_ms),
        )

    def _guarded_timer_now_ms(self, source_timer: dict[str, Any]) -> int:
        try:
            now_ms = self.store.effective_timer_now_ms(source_timer)
        except (OSError, sqlite3.Error, SharedCoreError) as error:
            # D48-07: shared tick/render clock budget; keep last-known.
            if should_capture_clock_failure(error):
                capture_exception(error)
            return self._fallback_timer_now_ms()
        except ValueError:
            return self._fallback_timer_now_ms()
        note_clock_recovery()
        self._last_timer_now_ms = now_ms
        return now_ms

    def _fallback_timer_now_ms(self) -> int:
        cached = getattr(self, "_last_timer_now_ms", None)
        if isinstance(cached, int):
            return cached
        try:
            return max(1, int(time.time() * 1000))
        except (OSError, ValueError, TypeError, OverflowError):
            return 1

    def _preview_completed_phase(
        self, source_timer: dict[str, Any], now_ms: int
    ) -> str | None:
        if source_timer.get("status") != "completed":
            return None
        if (source_timer.get("lastIntent") or {}).get("type") == "finish":
            return None
        if self._selected_phase() != source_timer.get("phase"):
            return None
        return self.store.preview_selected_phase(
            source_timer,
            self.history,
            bool(self.settings.get("autoStartBreaks")),
            now_ms,
        )

    def _render_task_selector(
        self,
        timer: dict[str, Any],
        active: bool,
    ) -> None:
        self.timer_screen.render_task_selector(
            timer,
            active,
            selected_phase=self._selected_phase(),
            settings=self.settings,
            tasks=self.tasks,
            known_tasks=self.known_tasks,
            mutations_enabled=not self._history_resolution_active,
        )

    def _render_history(self) -> None:
        self.arrivals_screen.render(self.history, self.known_tasks)

    def _render_network(self) -> None:
        if not hasattr(self, "network_screen"):
            return
        room = self._cached_iroh_room_value()
        available = self.iroh is not None and self.iroh.availability()[0]
        reason = self._iroh_unavailable_reason(available)
        self.network_screen.render(
            replication_mode=self.replication_mode,
            iroh_status=self._iroh_status,
            iroh_details=self._iroh_details,
            invite=self._iroh_invite,
            room=room,
            available=available,
            unavailable_reason=reason,
            cloud_authenticated=self.cloud.authenticated,
            cloud_deleting_account=self.cloud.deleting_account,
        )
        self.account_button.setEnabled(self.replication_mode == "centralized")
        if self.replication_mode != "centralized":
            self.account_button.setToolTip(
                self.strings.text("network.account_inactive")
            )
            self.account_button.setAccessibleName(
                self.strings.text("network.account_inactive_accessible")
            )

    def _cached_iroh_room_value(self) -> Any:
        try:
            room = self.store.iroh_room()
        except SecureStoreError as error:
            # R49-D08: infra-backed secure-store failures share the
            # room-read budget; malformed-data stays silent like validation.
            if should_capture_secure_store_error(error):
                if should_capture_room_read_failure(error):
                    capture_exception(error)
            return getattr(self, "_cached_iroh_room", None)
        except (OSError, sqlite3.Error) as error:
            # R49-D08: per-render room-read budget; reuse cached room.
            if should_capture_room_read_failure(error):
                capture_exception(error)
            return getattr(self, "_cached_iroh_room", None)
        except (ValueError, KeyError):
            # validation stays silent; reuse cached room.
            return getattr(self, "_cached_iroh_room", None)
        note_room_read_recovery()
        self._cached_iroh_room = room
        return room

    def _iroh_unavailable_reason(self, available: bool) -> str:
        if self.iroh is not None and not available:
            return self.iroh.availability()[1]
        if self.iroh is None:
            return self.strings.text("network.iroh_not_packaged")
        return ""

    def _render_tray(
        self,
        status: str,
        remaining: int,
        status_labels: dict[str, str],
    ) -> None:
        if not self.tray:
            return
        self.tray.setToolTip(
            self.strings.text(
                "tray.tooltip",
                remaining=format_remaining(remaining),
                status=status_labels.get(status, status),
            )
        )
        self.tray_primary.setText(
            self.strings.text("tray.primary.pause")
            if status == "running"
            else self.strings.text("tray.primary.resume")
            if status == "paused"
            else self.strings.text(
                "tray.primary.start",
                phase=self.strings.text(f"phase.{self._selected_phase()}").lower(),
            )
        )
        self.tray_primary.setEnabled(self.primary_button.isEnabled())

    def _render_completion_notification(
        self,
        source_timer: dict[str, Any],
        status: str,
    ) -> None:
        timer_id = source_timer.get("id")
        if (
            status != "completed"
            or timer_id in self.provisional_auto_break_timer_ids
            or timer_id == self._notified_timer_id
        ):
            return
        self._notified_timer_id = timer_id
        self._notify(
            self.strings.text("status.service_arrived"),
            self.strings.text(
                "status.service_completed",
                phase=self.strings.text(f"phase.{source_timer['phase']}"),
            ),
        )

    def _refresh_stylesheet(self) -> None:
        palette_key = QApplication.palette().cacheKey()
        font_scale = _app_font_scale()
        if palette_key == self._palette_key and font_scale == getattr(
            self, "_font_scale", None
        ):
            return
        self._palette_key = palette_key
        self._font_scale = font_scale
        self.setStyleSheet(
            "\n".join(  # noqa: FLY002 - screen styles compose independently
                (
                    self._stylesheet(),
                    TimerScreen.stylesheet(),
                    TasksScreen.stylesheet(),
                    ArrivalsScreen.stylesheet(),
                    NetworkScreen.stylesheet(),
                )
            )
        )

    @staticmethod
    def _stylesheet() -> str:
        scale = _app_font_scale()
        sheet = """
        QWidget#root { background: palette(window); color: palette(window-text); font-family: "Noto Sans", "Segoe UI", sans-serif; font-size: 12px; }
        QLabel#brand { color: palette(window-text); font-family: "DejaVu Sans Condensed"; font-size: 24px; font-weight: 900; letter-spacing: 2px; }
        QLabel#tagline, QLabel#device { color: palette(window-text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 2px; }
        QFrame#rule { color: palette(highlight); background: palette(highlight); max-height: 4px; min-height: 4px; border: 0; }
        QFrame#ticket { background: palette(base); color: palette(text); border: 3px solid palette(highlight); }
        QLabel#sectionTitle { color: palette(text); font-family: "DejaVu Sans Condensed"; font-size: 14px; font-weight: 900; letter-spacing: 1px; }
        QPushButton { min-height: 29px; background: palette(button); color: palette(text); border: 2px solid palette(text); padding: 3px 7px; font-weight: 800; }
        QPushButton:hover { border-color: palette(highlight); }
        QPushButton:pressed { padding-top: 5px; padding-left: 9px; }
        QPushButton:focus { border: 3px solid palette(highlight); }
        QPushButton:disabled { color: palette(window-text); border-color: palette(dark); background: palette(midlight); }
        QPushButton#primaryButton:disabled { color: palette(window-text); border-color: palette(dark); background: palette(midlight); }
        QPushButton#primaryButton { background: palette(highlight); color: palette(highlighted-text); min-height: 36px; font-size: 14px; }
        QPushButton#accountButton { border-color: palette(highlight); min-width: 72px; max-width: 150px; }
        QPushButton#accountButton[authenticated="true"] { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0; }
        QPushButton[screen="true"] { min-width: 64px; font-family: "DejaVu Sans Condensed"; letter-spacing: 1px; }
        QPushButton[screen="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QToolButton#settingsButton { font-size: 24px; }
        QLabel#microLabel { color: palette(text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QLabel#taskSubtitle { color: palette(text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QComboBox, QLineEdit, QPlainTextEdit { min-height: 29px; background: palette(base); color: palette(text); border: 2px solid palette(mid); padding: 2px 6px; }
        QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus { border: 3px solid palette(highlight); }
        QStatusBar { background: palette(window); color: palette(window-text); }
        """
        sheet = (
            sheet.replace("font-size: 12px", f"font-size: {9.0 * scale:.1f}pt")
            .replace("font-size: 24px", f"font-size: {18.0 * scale:.1f}pt")
            .replace("font-size: 14px", f"font-size: {10.5 * scale:.1f}pt")
            .replace("font-size: 9px", f"font-size: {6.75 * scale:.1f}pt")
        )
        # R52-D04: scale each pixel dimension once. Chained str.replace
        # calls re-scaled values just created (36px->72px, then 72px->144px).
        return re.sub(
            r"\b(29|36|64|72|150)px\b",
            lambda match: f"{round(int(match.group(1)) * scale)}px",
            sheet,
        )
