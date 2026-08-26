from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .arrivals_screen import ArrivalsScreen
from .core import format_remaining
from .network_screen import PRIVACY_POLICY_URL as PRIVACY_POLICY_URL, NetworkScreen
from .tasks_screen import TasksScreen
from .timer_screen import TimerRenderState, TimerScreen


class ScreenNavigation(QWidget):
    screen_requested = Signal(int)

    def __init__(self, strings: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
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
            layout.addWidget(button)
        self.buttons[0].setChecked(True)

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
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel(self.strings.text("brand.name"))
        title.setObjectName("brand")
        tagline = QLabel(self.strings.text("brand.tagline"))
        tagline.setObjectName("tagline")
        brand.addWidget(title)
        brand.addWidget(tagline)
        header.addLayout(brand)
        self.navigation = ScreenNavigation(self.strings, self)
        self.screen_group = self.navigation.group
        self.screen_buttons = self.navigation.buttons
        header.addWidget(self.navigation)
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
            self.tasks_screen,
            self.arrivals_screen,
            self.network_screen,
        )

    def _expose_timer_seams(self) -> None:
        self.timer_page = self.timer_screen
        names = (
            "content_layout",
            "left_layout",
            "clock",
            "long_break_progress",
            "active_task_context",
            "actions_layout",
            "task_selector_panel",
            "task_combo",
            "primary_button",
            "finish_button",
            "cancel_button",
            "stop_sound_button",
            "right_panel",
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
        self.tasks_page = self.tasks_screen
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
        timer.stop_sound_requested.connect(self._stop_sound_and_clear)
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
        self.shortcuts[0].activated.connect(self._primary_action)
        self.shortcuts[1].activated.connect(lambda: self._issue("finish"))
        for index, shortcut in enumerate(self.shortcuts[2:]):
            shortcut.activated.connect(
                lambda page=index: self.navigation.screen_requested.emit(page)
            )

    def _settings_toggled(self, visible: bool) -> None:
        self.timer_screen.set_settings_visible(visible)
        label = self.strings.text(
            "status.settings_hide" if visible else "status.settings_show"
        )
        self.settings_button.setToolTip(label)
        self.settings_button.setAccessibleName(label)

    def _display_screen(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        self.screen_buttons[index].setChecked(True)
        self.settings_button.setVisible(index == 0)
        self._render()
        focus_targets = (
            self.primary_button,
            self.task_input,
            self.history_list,
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
        return self.timer_screen.presentation(
            source_timer,
            selected_phase=self._selected_phase(),
            settings=self.settings,
            now_ms=self.store.effective_timer_now_ms(source_timer),
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
        room = self.store.iroh_room()
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
        if palette_key == self._palette_key:
            return
        self._palette_key = palette_key
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
        return """
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
        QPushButton:disabled { color: palette(text); border-color: palette(text); background: palette(button); }
        QPushButton#primaryButton { background: palette(highlight); color: palette(highlighted-text); min-height: 36px; font-size: 14px; }
        QPushButton#accountButton { border-color: palette(highlight); min-width: 72px; max-width: 150px; }
        QPushButton#accountButton[authenticated="true"] { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0; }
        QPushButton[screen="true"] { min-width: 64px; font-family: "DejaVu Sans Condensed"; letter-spacing: 1px; }
        QPushButton[screen="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QToolButton#settingsButton { font-size: 24px; }
        QLabel#microLabel { color: palette(text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QLabel#taskSubtitle { color: palette(mid); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QComboBox, QLineEdit, QPlainTextEdit { min-height: 29px; background: palette(base); color: palette(text); border: 2px solid palette(mid); padding: 2px 6px; }
        QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus { border: 3px solid palette(highlight); }
        QStatusBar { background: palette(window); color: palette(window-text); }
        """
