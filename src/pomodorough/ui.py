from __future__ import annotations

import math
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .core import (
    ACTIVE_STATUSES,
    PHASES,
    TERMINAL_STATUSES,
    completed_focus_count_for_day,
    elapsed_ms,
    empty_timer,
    format_remaining,
    long_break_progress,
    rebuild_optimistic,
    rebuild_tasks,
    task_from_title,
    task_summaries_today,
    timer_for_display,
)
from .network import CloudService
from .iroh_protocol import IrohProtocolError, parse_invite
from .localization import Strings
from .storage import Store


PRIVACY_POLICY_URL = "https://pomodoro-everywhere.github.io/pomodorough-server/privacy/"


def resource_path(name: str) -> Path:
    return Path(__file__).parent / "resources" / name


class ClockWidget(QWidget):
    def __init__(self, strings: Strings | None = None) -> None:
        super().__init__()
        self.strings = strings or Strings()
        self.time_text = "25:00"
        self.phase_text = self.strings.text("phase.focus").upper()
        self.status_text = self.strings.text("status.rail.idle")
        self.progress = 0.0
        self.setAccessibleName(self.strings.text("status.timer_accessible"))
        self.setMinimumSize(170, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(360, 360)

    def set_state(self, time_text: str, phase: str, status: str, progress: float) -> None:
        self.time_text = time_text
        self.phase_text = phase.upper()
        self.status_text = status.upper()
        self.progress = max(0.0, min(1.0, progress))
        self.setAccessibleDescription(self.strings.text(
            "status.timer_description", phase=phase.title(), time=time_text,
            status=status.lower()
        ))
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 14
        dial = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        center = dial.center()
        radius = side / 2
        palette = self.palette()
        base = palette.color(QPalette.ColorRole.Base)
        alternate = palette.color(QPalette.ColorRole.AlternateBase)
        text = palette.color(QPalette.ColorRole.Text)
        window = palette.color(QPalette.ColorRole.Window)
        window_text = palette.color(QPalette.ColorRole.WindowText)
        mid = palette.color(QPalette.ColorRole.Mid)
        highlight = palette.color(QPalette.ColorRole.Highlight)

        painter.setPen(QPen(base, max(6, side * 0.022)))
        painter.setBrush(alternate)
        painter.drawEllipse(dial.adjusted(7, 7, -7, -7))
        painter.setPen(QPen(mid, max(10, side * 0.045)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(dial.adjusted(radius * 0.17, radius * 0.17, -radius * 0.17, -radius * 0.17))

        painter.save()
        painter.translate(center)
        painter.setPen(QPen(text, max(3, side * 0.009)))
        for tick in range(60):
            length = radius * (0.11 if tick % 5 == 0 else 0.055)
            outer = radius * 0.81
            painter.drawLine(QPointF(0, -outer), QPointF(0, -outer + length))
            painter.rotate(6)
        painter.restore()

        arc_rect = dial.adjusted(radius * 0.18, radius * 0.18, -radius * 0.18, -radius * 0.18)
        painter.setPen(QPen(highlight, max(10, side * 0.047), Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(arc_rect, 90 * 16, -round(self.progress * 360 * 16))

        angle = math.radians(-90 + self.progress * 360)
        pointer_radius = radius * 0.64
        pointer = QPointF(
            center.x() + math.cos(angle) * pointer_radius,
            center.y() + math.sin(angle) * pointer_radius,
        )
        painter.setPen(QPen(text, max(4, side * 0.013)))
        painter.drawLine(center, pointer)
        painter.setBrush(highlight)
        painter.drawEllipse(pointer, max(5, side * 0.018), max(5, side * 0.018))

        board = QRectF(center.x() - radius * 0.79, center.y() - radius * 0.27, radius * 1.58, radius * 0.54)
        painter.setPen(QPen(base, max(4, side * 0.013)))
        painter.setBrush(text)
        painter.drawRect(board)
        painter.setPen(QPen(window, 3))
        painter.drawLine(center.x(), board.top() + 5, center.x(), board.bottom() - 5)

        display_font = QFont("DejaVu Sans Mono", max(20, round(side * 0.105)), QFont.Weight.Bold)
        display_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 91)
        painter.setFont(display_font)
        painter.setPen(base)
        painter.drawText(board, Qt.AlignmentFlag.AlignCenter, self.time_text)

        label_font = QFont("DejaVu Sans Condensed", max(8, round(side * 0.032)), QFont.Weight.Bold)
        label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        painter.setFont(label_font)
        painter.setPen(window_text)
        phase_rect = QRectF(dial.left(), board.top() - radius * 0.24, side, radius * 0.18)
        painter.drawText(phase_rect, Qt.AlignmentFlag.AlignCenter, self.phase_text)
        status_width = min(
            radius * 1.6,
            painter.fontMetrics().horizontalAdvance(self.status_text) + radius * 0.18,
        )
        status_rect = QRectF(
            center.x() - status_width / 2,
            board.bottom() + radius * 0.06,
            status_width,
            radius * 0.18,
        )
        painter.fillRect(status_rect, alternate)
        painter.setPen(window_text)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignCenter, self.status_text)


class MainWindow(QMainWindow):
    notice = Signal(str)

    def __init__(
        self,
        store: Store,
        cloud: CloudService,
        app_icon: QIcon,
        iroh: Any | None = None,
        locale: str | None = None,
    ) -> None:
        super().__init__()
        self.strings = Strings(locale)
        self.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
            if self.strings.is_rtl
            else Qt.LayoutDirection.LeftToRight
        )
        self.store = store
        self.cloud = cloud
        self.cloud.strings = self.strings
        self.app_icon = app_icon
        self.iroh = iroh
        self.replication_mode = store.replication_mode
        self._iroh_status = self.strings.text("network.not_connected")
        self._iroh_details: dict[str, Any] = {}
        self._iroh_invite = ""
        self._cloud_restore_after_iroh_stop = False
        self._iroh_join_pending = False
        self.quitting = False
        self._closed = False
        self._notified_timer_id: str | None = None
        self._sound_active = False
        self.sound_timer = QTimer(self)
        self.sound_timer.setInterval(1_200)
        self.sound_timer.timeout.connect(QApplication.beep)
        self._auto_finish_in_progress = False
        self._auto_break_not_before = 0.0
        self._tray_progress_state: tuple[bool, int] | None = None
        self._palette_key: int | None = None
        self._compact: bool | None = None
        self._landscape: bool | None = None
        self._account_synced = False
        self._sync_request: dict[str, Any] | None = None
        self._sync_waiting = False
        self._history_resolution_active = False
        self._resolution_user: dict[str, Any] | None = None
        self._resolution_phase: str | None = None
        self._resolution_preview: dict[str, Any] | None = None
        self._resolution_request_id: str | None = None
        self._resolution_retry_paused = False
        self._resolution_retry_scheduled = False
        self._resolution_corruption: str | None = None
        self._account_switch_user: dict[str, Any] | None = None
        self._task_selector_signature: tuple[Any, ...] | None = None
        self._task_render_signature: tuple[Any, ...] | None = None
        self._load_state()
        self._activate_persisted_resolution()
        self.setWindowTitle(self.strings.text("window.title"))
        self.setWindowIcon(app_icon)
        self.setMinimumSize(600, 340)
        self.resize(640, 360)
        self._build_ui()
        self._build_tray()
        self._connect_cloud()
        self._connect_iroh()
        self._render()

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(250)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self._sync)
        self.sync_timer.start(15_000)
        QTimer.singleShot(0, self._restore_replication)

    def _load_state(self) -> None:
        previous_timer = getattr(self, "timer", None)
        state, provisional_timer_ids = (
            self.store.load_with_provisional_auto_breaks()
        )
        try:
            self.store.pending_resolution()
            self._resolution_corruption = None
        except ValueError as error:
            self._resolution_corruption = str(error)
        self.settings = state["settings"]
        self.revision = int(state["snapshot"].get("revision", 0))
        projection_snapshot = state.get("projectionSnapshot", state["snapshot"])
        self.base_timer = projection_snapshot.get("canonicalTimer")
        self.base_history = projection_snapshot.get("history", [])
        self.base_tasks = state["snapshot"].get("tasks", [])
        self.known_tasks = {
            task["id"]: task
            for task in state["snapshot"].get("knownTasks", [])
            if task.get("id") and task.get("title")
        }
        self.user = state["snapshot"].get("user")
        self.pending = state["pending"]
        self.pending_tasks = state["pendingTasks"]
        self.pending_durations = state["pendingDurations"]
        self.pending_auto_starts = state["pendingAutoStarts"]
        self.timer, self.history = rebuild_optimistic(
            self.base_timer,
            self.base_history,
            state.get("projectionPending", self.pending),
        )
        if (
            self._sound_active
            and previous_timer
            and self.timer
            and previous_timer.get("id") != self.timer.get("id")
            and previous_timer.get("status") in TERMINAL_STATUSES
            and self.timer.get("status") in ACTIVE_STATUSES
        ):
            self._stop_sound()
        self.provisional_auto_break_timer_ids = (
            provisional_timer_ids if self.user is not None else set()
        )
        if (
            previous_timer
            and self.timer
            and previous_timer.get("id") == self.timer.get("id")
            and (
                previous_timer.get("phase") != self.timer.get("phase")
                or previous_timer.get("status") in TERMINAL_STATUSES
                and self.timer.get("status") in ACTIVE_STATUSES
            )
        ):
            self._notified_timer_id = None
        self.tasks = rebuild_tasks(self.base_tasks, self.pending_tasks)
        for task in self.tasks:
            self.known_tasks[task["id"]] = task
        self._task_selector_signature = None
        self._task_render_signature = None
        if hasattr(self, "duration_spins"):
            self._refresh_duration_spins()
        if hasattr(self, "auto_breaks"):
            previous = self.auto_breaks.blockSignals(True)
            self.auto_breaks.setChecked(
                bool(self.settings.get("autoStartBreaks"))
            )
            self.auto_breaks.blockSignals(previous)

    def _activate_persisted_resolution(self) -> bool:
        try:
            pending = self.store.pending_resolution()
        except ValueError as error:
            self._resolution_corruption = str(error)
            self._history_resolution_active = True
            self._resolution_user = self.user
            self._resolution_phase = None
            self._resolution_preview = None
            self._resolution_request_id = None
            self._resolution_retry_paused = True
            return True
        if pending is None:
            return False
        self._history_resolution_active = True
        self._resolution_user = pending["owner"]
        self._resolution_phase = "resolve"
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        return True

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        self.outer_layout = QVBoxLayout(root)

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
        self.screen_group = QButtonGroup(self)
        self.screen_group.setExclusive(True)
        self.screen_buttons: list[QPushButton] = []
        destinations = (
            ("timer", self.strings.text("nav.timer")),
            ("tasks", self.strings.text("nav.tasks")),
            ("arrivals", self.strings.text("nav.arrivals")),
            ("network", self.strings.text("nav.network")),
        )
        for index, (destination, label) in enumerate(destinations):
            button = QPushButton(label)
            button.setAccessibleName(
                self.strings.text(
                    "nav.show",
                    destination=self.strings.text(f"destination.{destination}"),
                )
            )
            button.setCheckable(True)
            button.setProperty("screen", True)
            button.clicked.connect(
                lambda checked=False, page=index: self._show_screen(page)
            )
            self.screen_group.addButton(button)
            self.screen_buttons.append(button)
            header.addWidget(button)
        self.screen_buttons[0].setChecked(True)
        header.addStretch()
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setCheckable(True)
        self.settings_button.setMinimumSize(40, 40)
        self.settings_button.setIconSize(QSize(28, 28))
        settings_icon = QIcon.fromTheme("settings-configure")
        if settings_icon.isNull():
            self.settings_button.setText("⚙")
            self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        else:
            self.settings_button.setIcon(settings_icon)
            self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.settings_button.setToolTip(self.strings.text("status.settings_show"))
        self.settings_button.setAccessibleName(self.strings.text("status.settings_show"))
        self.settings_button.toggled.connect(self._settings_toggled)
        self.account_button = QPushButton(self.strings.text("account.sign_in"))
        self.account_button.setObjectName("accountButton")
        self.account_button.clicked.connect(self._account_action)
        header.addWidget(self.settings_button)
        header.addWidget(self.account_button)
        self.outer_layout.addLayout(header)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setObjectName("rule")
        self.outer_layout.addWidget(rule)

        self.page_stack = QStackedWidget()
        self.timer_page = QWidget()
        self.content_layout = QHBoxLayout(self.timer_page)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.clock = ClockWidget(self.strings)
        self.left_layout.addWidget(self.clock, 1)
        self.long_break_progress = QLabel("○○○○")
        self.long_break_progress.setObjectName("microLabel")
        self.long_break_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.long_break_progress.setAccessibleName(self.strings.text("status.pomodoro_progress"))
        self.left_layout.addWidget(self.long_break_progress)
        self.active_task_context = QLabel("")
        self.active_task_context.setObjectName("taskSubtitle")
        self.active_task_context.setAccessibleName(self.strings.text("task.active_accessible"))
        self.active_task_context.setWordWrap(True)
        self.left_layout.addWidget(self.active_task_context)

        self.actions_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.actions_layout.setSpacing(8)
        self.task_selector_panel = QFrame()
        self.task_selector_panel.setObjectName("taskSelector")
        self.task_selector_panel.setMinimumWidth(190)
        self.task_selector_panel.setMaximumHeight(76)
        task_selector_layout = QHBoxLayout(self.task_selector_panel)
        task_selector_layout.setContentsMargins(8, 5, 8, 7)
        task_selector_layout.setSpacing(8)
        task_selector_label = QLabel(self.strings.text("task.focus").upper())
        task_selector_label.setObjectName("microLabel")
        self.task_combo = QComboBox()
        self.task_combo.setAccessibleName(self.strings.text("task.focus"))
        self.task_combo.currentIndexChanged.connect(self._task_selection_changed)
        task_selector_layout.addWidget(task_selector_label)
        task_selector_layout.addWidget(self.task_combo, 1)
        self.primary_button = QPushButton(self.strings.text("status.primary.start", phase=self.strings.text("phase.focus").upper()))
        self.primary_button.setObjectName("primaryButton")
        self.primary_button.clicked.connect(self._primary_action)
        self.finish_button = QPushButton(self.strings.text("status.finish"))
        self.finish_button.clicked.connect(lambda: self._issue("finish"))
        self.cancel_button = QPushButton(self.strings.text("status.cancel"))
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(lambda: self._issue("cancel"))
        self.stop_sound_button = QPushButton(self.strings.text("status.stop_sound"))
        self.stop_sound_button.clicked.connect(self._stop_sound_and_clear)
        self.stop_sound_button.setVisible(False)
        self.actions_layout.addWidget(self.task_selector_panel)
        self.actions_layout.addWidget(self.primary_button, 1)
        self.actions_layout.addWidget(self.finish_button, 1)
        self.actions_layout.addWidget(self.cancel_button, 1)
        self.actions_layout.addWidget(self.stop_sound_button, 1)
        self.left_layout.addLayout(self.actions_layout)
        self.content_layout.addLayout(self.left_layout, 3)

        self.right_panel = QFrame()
        self.right_panel.setObjectName("ticket")
        self.right_layout = QVBoxLayout(self.right_panel)

        service_label = QLabel(self.strings.text("pattern.title"))
        service_label.setObjectName("sectionTitle")
        self.right_layout.addWidget(service_label)
        service_detail = QLabel(self.strings.text("pattern.detail"))
        service_detail.setObjectName("taskSubtitle")
        self.right_layout.addWidget(service_detail)
        self.pattern_scope = QLabel("")
        self.pattern_scope.setObjectName("taskSubtitle")
        self.pattern_scope.setAccessibleName(self.strings.text("pattern.next_scope"))
        self.right_layout.addWidget(self.pattern_scope)
        self.phase_group = QButtonGroup(self)
        self.phase_group.setExclusive(True)
        self.phase_buttons: dict[str, QPushButton] = {}
        phase_layout = QHBoxLayout()
        phase_layout.setSpacing(5)
        for phase, definition in PHASES.items():
            phase_label = self.strings.text(f"phase.{phase}")
            button = QPushButton(phase_label.upper())
            button.setToolTip(phase_label)
            button.setCheckable(True)
            button.setProperty("phase", True)
            button.clicked.connect(lambda checked=False, value=phase: self._select_phase(value))
            self.phase_group.addButton(button)
            self.phase_buttons[phase] = button
            phase_layout.addWidget(button)
        self.right_layout.addLayout(phase_layout)

        duration_grid = QGridLayout()
        duration_grid.setVerticalSpacing(7)
        self.duration_spins: dict[str, QSpinBox] = {}
        for row, (phase, definition) in enumerate(PHASES.items()):
            phase_label = self.strings.text(f"phase.{phase}")
            label = QLabel(phase_label)
            spin = QSpinBox()
            spin.setAccessibleName(self.strings.text("pattern.duration_accessible", phase=phase_label))
            spin.setRange(1, 180)
            spin.setSuffix(self.strings.text("pattern.minutes_suffix"))
            spin.setValue(int(self.settings["durations"][phase]))
            spin.valueChanged.connect(lambda value, key=phase: self._duration_changed(key, value))
            duration_grid.addWidget(label, row, 0)
            duration_grid.addWidget(spin, row, 1)
            self.duration_spins[phase] = spin
        self.right_layout.addLayout(duration_grid)
        self.auto_breaks = QCheckBox(self.strings.text("pattern.auto_breaks"))
        self.auto_breaks.setChecked(bool(self.settings.get("autoStartBreaks")))
        self.auto_breaks.toggled.connect(self._auto_breaks_changed)
        self.right_layout.addWidget(self.auto_breaks)
        auto_breaks_detail = QLabel(self.strings.text("pattern.auto_breaks_detail"))
        auto_breaks_detail.setObjectName("taskSubtitle")
        auto_breaks_detail.setWordWrap(True)
        self.right_layout.addWidget(auto_breaks_detail)
        self.alert_guarantee = QLabel(self.strings.text("pattern.alert_guarantee"))
        self.alert_guarantee.setObjectName("privacyNotice")
        self.alert_guarantee.setWordWrap(True)
        self.alert_guarantee.setAccessibleName(self.strings.text("pattern.alert_accessible"))
        self.right_layout.addWidget(self.alert_guarantee)
        self.right_layout.addStretch()
        self.content_layout.addWidget(self.right_panel, 2)
        self.right_panel.hide()
        self.page_stack.addWidget(self.timer_page)
        self.tasks_page = self._build_tasks_page()
        self.page_stack.addWidget(self.tasks_page)
        self.arrivals_page = self._build_arrivals_page()
        self.page_stack.addWidget(self.arrivals_page)
        self.network_page = self._build_network_page()
        self.page_stack.addWidget(self.network_page)
        self.outer_layout.addWidget(self.page_stack, 1)

        self.shortcuts = [
            QShortcut(QKeySequence(Qt.Key.Key_Space), self),
            QShortcut(QKeySequence("Ctrl+Shift+F"), self),
            *(QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self) for index in range(4)),
        ]
        self.shortcuts[0].activated.connect(self._primary_action)
        self.shortcuts[1].activated.connect(lambda: self._issue("finish"))
        for index, shortcut in enumerate(self.shortcuts[2:]):
            shortcut.activated.connect(
                lambda page=index: self._show_screen(page)
            )
        self.notice.connect(self._show_notice)
        self._refresh_stylesheet()
        self._apply_responsive_layout()

    def _build_tasks_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("ticket")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.strings.text("task.board"))
        title.setObjectName("sectionTitle")
        subtitle = QLabel(self.strings.text("task.board_detail"))
        subtitle.setObjectName("taskSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.task_totals = QLabel(self.strings.text("task.totals", count=0, unit=self.strings.text("task.pomodoro.other"), minutes=self.strings.text("duration.minutes", minutes=0).upper()))
        self.task_totals.setObjectName("countBadge")
        header.addWidget(self.task_totals)
        layout.addLayout(header)

        form = QHBoxLayout()
        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText(self.strings.text("task.placeholder"))
        self.task_input.setAccessibleName(self.strings.text("task.name_accessible"))
        self.task_input.returnPressed.connect(self._add_task)
        self.add_task_button = QPushButton(self.strings.text("task.add"))
        self.add_task_button.setObjectName("primaryButton")
        self.add_task_button.clicked.connect(self._add_task)
        form.addWidget(self.task_input, 1)
        form.addWidget(self.add_task_button)
        layout.addLayout(form)

        self.task_table = QTableWidget(0, 4)
        self.task_table.setAccessibleName(self.strings.text("task.board_accessible"))
        self.task_table.setHorizontalHeaderLabels(
            tuple(self.strings.text(f"task.column.{key}") for key in ("task", "finished", "time", "action"))
        )
        for column, alignment in enumerate(
            (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignCenter,
                Qt.AlignmentFlag.AlignCenter,
                Qt.AlignmentFlag.AlignCenter,
            )
        ):
            self.task_table.horizontalHeaderItem(column).setTextAlignment(alignment)
        self.task_table.verticalHeader().hide()
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.task_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        table_header = self.task_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.task_table, 1)
        self.tasks_empty = QLabel(self.strings.text("task.empty"))
        self.tasks_empty.setObjectName("emptyState")
        self.tasks_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.tasks_empty)
        return page

    def _build_arrivals_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("ticket")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

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
        layout.addLayout(header)

        self.history_list = QListWidget()
        self.history_list.setAccessibleName(self.strings.text("arrivals.accessible"))
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.history_list, 1)
        self.device_label = QLabel(self.strings.text("device.label", device=self.store.device_id[-8:].upper()))
        self.device_label.setObjectName("device")
        layout.addWidget(self.device_label)
        return page

    def _build_network_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("ticket")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.strings.text("network.title"))
        title.setObjectName("sectionTitle")
        subtitle = QLabel(self.strings.text("network.detail"))
        subtitle.setObjectName("taskSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.network_status = QLabel(self.strings.text("network.not_connected"))
        self.network_status.setObjectName("countBadge")
        self.network_status.setAccessibleName(self.strings.text("network.status_accessible"))
        header.addWidget(self.network_status)
        layout.addLayout(header)

        mode_row = QHBoxLayout()
        mode_label = QLabel(self.strings.text("network.route"))
        mode_label.setObjectName("microLabel")
        self.replication_mode_combo = QComboBox()
        self.replication_mode_combo.setAccessibleName(self.strings.text("network.mode_accessible"))
        self.replication_mode_combo.addItem(self.strings.text("network.mode.offline"), "offline")
        self.replication_mode_combo.addItem(self.strings.text("network.mode.iroh"), "iroh")
        self.replication_mode_combo.addItem(self.strings.text("network.mode.centralized"), "centralized")
        self.replication_mode_combo.setCurrentIndex(
            max(0, self.replication_mode_combo.findData(self.replication_mode))
        )
        self.replication_mode_combo.currentIndexChanged.connect(
            self._replication_mode_changed
        )
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.replication_mode_combo, 1)
        layout.addLayout(mode_row)

        account_row = QHBoxLayout()
        account_label = QLabel(self.strings.text("account.heading"))
        account_label.setObjectName("microLabel")
        self.privacy_policy_button = QPushButton(
            self.strings.text("account.privacy_policy")
        )
        self.privacy_policy_button.setAccessibleDescription(PRIVACY_POLICY_URL)
        self.privacy_policy_button.clicked.connect(self._open_privacy_policy)
        self.delete_account_button = QPushButton(self.strings.text("account.delete"))
        self.delete_account_button.setObjectName("dangerButton")
        self.delete_account_button.clicked.connect(self._delete_account_action)
        account_row.addWidget(account_label)
        account_row.addStretch()
        account_row.addWidget(self.privacy_policy_button)
        account_row.addWidget(self.delete_account_button)
        layout.addLayout(account_row)

        self.network_unavailable = QLabel("")
        self.network_unavailable.setObjectName("privacyNotice")
        self.network_unavailable.setWordWrap(True)
        self.network_unavailable.setAccessibleName(self.strings.text("network.iroh_unavailable_accessible"))
        layout.addWidget(self.network_unavailable)

        self.iroh_first_room_guidance = QLabel(
            self.strings.text("iroh.first_room_guidance")
        )
        self.iroh_first_room_guidance.setObjectName("privacyNotice")
        self.iroh_first_room_guidance.setWordWrap(True)
        self.iroh_first_room_guidance.setAccessibleName(
            self.strings.text("iroh.first_room_accessible")
        )
        layout.addWidget(self.iroh_first_room_guidance)

        self.iroh_panel = QFrame()
        self.iroh_panel.setObjectName("networkPanel")
        iroh_layout = QGridLayout(self.iroh_panel)
        iroh_layout.setContentsMargins(12, 12, 12, 12)
        iroh_layout.setHorizontalSpacing(8)
        iroh_layout.setVerticalSpacing(8)

        self.room_name_input = QLineEdit()
        self.room_name_input.setPlaceholderText(self.strings.text("network.room_name_placeholder"))
        self.room_name_input.setMaxLength(64)
        self.room_name_input.setAccessibleName(self.strings.text("network.room_name_accessible"))
        self.create_room_button = QPushButton(self.strings.text("network.create_room"))
        self.create_room_button.setObjectName("primaryButton")
        self.create_room_button.setAccessibleName(self.strings.text("network.create_room_accessible"))
        self.create_room_button.clicked.connect(self._create_iroh_room)
        iroh_layout.addWidget(self.room_name_input, 0, 0)
        iroh_layout.addWidget(self.create_room_button, 0, 1)

        self.invite_input = QPlainTextEdit()
        self.invite_input.setPlaceholderText(self.strings.text("network.invite_placeholder"))
        self.invite_input.setAccessibleName(self.strings.text("network.invite_accessible"))
        self.invite_input.setMaximumHeight(70)
        self.join_room_button = QPushButton(self.strings.text("network.join_room"))
        self.join_room_button.setAccessibleName(self.strings.text("network.join_room_accessible"))
        self.join_room_button.clicked.connect(self._join_iroh_room)
        iroh_layout.addWidget(self.invite_input, 1, 0)
        iroh_layout.addWidget(self.join_room_button, 1, 1)

        self.invite_output = QPlainTextEdit()
        self.invite_output.setReadOnly(True)
        self.invite_output.setPlaceholderText(self.strings.text("network.invite_output_placeholder"))
        self.invite_output.setAccessibleName(self.strings.text("network.invite_output_accessible"))
        self.invite_output.setMaximumHeight(70)
        self.copy_invite_button = QPushButton(self.strings.text("network.copy_invite"))
        self.copy_invite_button.setAccessibleName(self.strings.text("network.copy_invite_accessible"))
        self.copy_invite_button.clicked.connect(self._copy_iroh_invite)
        iroh_layout.addWidget(self.invite_output, 2, 0)
        iroh_layout.addWidget(self.copy_invite_button, 2, 1)

        action_row = QHBoxLayout()
        self.refresh_invite_button = QPushButton(self.strings.text("network.refresh_ticket"))
        self.refresh_invite_button.clicked.connect(self._refresh_iroh_invite)
        self.sync_iroh_button = QPushButton(self.strings.text("network.sync_now"))
        self.sync_iroh_button.clicked.connect(self._sync_iroh_now)
        self.leave_room_button = QPushButton(self.strings.text("network.leave_room"))
        self.leave_room_button.setObjectName("dangerButton")
        self.leave_room_button.clicked.connect(self._leave_iroh_room)
        action_row.addWidget(self.refresh_invite_button)
        action_row.addWidget(self.sync_iroh_button)
        action_row.addStretch()
        action_row.addWidget(self.leave_room_button)
        iroh_layout.addLayout(action_row, 3, 0, 1, 2)
        layout.addWidget(self.iroh_panel)

        self.network_details = QLabel(self.strings.text("network.no_room"))
        self.network_details.setObjectName("device")
        self.network_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.network_details.setAccessibleName(self.strings.text("network.details_accessible"))
        layout.addWidget(self.network_details)

        privacy = QLabel(self.strings.text("network.privacy"))
        privacy.setObjectName("privacyNotice")
        privacy.setWordWrap(True)
        privacy.setAccessibleName(self.strings.text("network.privacy_accessible"))
        layout.addWidget(privacy)
        layout.addStretch()
        return page

    def _refresh_stylesheet(self) -> None:
        palette_key = QApplication.palette().cacheKey()
        if palette_key == self._palette_key:
            return
        self._palette_key = palette_key
        self.setStyleSheet(self._stylesheet())

    def _settings_toggled(self, visible: bool) -> None:
        self.right_panel.setVisible(visible)
        label = self.strings.text("status.settings_hide" if visible else "status.settings_show")
        self.settings_button.setToolTip(label)
        self.settings_button.setAccessibleName(label)

    def _show_screen(self, index: int, *, sync: bool = True) -> None:
        self.page_stack.setCurrentIndex(index)
        self.screen_buttons[index].setChecked(True)
        self.settings_button.setVisible(index == 0)
        self._render()
        focus_targets = (self.primary_button, self.task_input, self.history_list)
        if index < len(focus_targets):
            focus_targets[index].setFocus(Qt.FocusReason.ShortcutFocusReason)
        if sync and index and self.replication_mode == "centralized":
            self._sync()

    def _apply_responsive_layout(self) -> None:
        landscape = self.width() > self.height()
        if landscape != self._landscape:
            self._landscape = landscape
            self.left_layout.setDirection(
                QBoxLayout.Direction.LeftToRight
                if landscape
                else QBoxLayout.Direction.TopToBottom
            )
            self.actions_layout.setDirection(
                QBoxLayout.Direction.TopToBottom
                if landscape
                else QBoxLayout.Direction.LeftToRight
            )

        compact = self.width() < 780 or self.height() < 540
        if compact == self._compact:
            return
        self._compact = compact
        self.outer_layout.setContentsMargins(
            12 if compact else 24,
            10 if compact else 18,
            12 if compact else 24,
            12 if compact else 22,
        )
        self.outer_layout.setSpacing(8 if compact else 15)
        self.content_layout.setSpacing(12 if compact else 22)
        self.left_layout.setSpacing(6 if compact else 10)
        margin = 12 if compact else 18
        self.right_layout.setContentsMargins(margin, margin, margin, margin)
        self.right_layout.setSpacing(7 if compact else 12)
        self.right_panel.setMinimumWidth(225 if compact else 310)
        self.right_panel.setMaximumWidth(300 if compact else 370)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "arrivals_page"):
            self._apply_responsive_layout()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "clock"):
            self._refresh_stylesheet()
            self._tray_progress_state = None
            self.clock.update()
            if hasattr(self, "tray"):
                self._render()

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip("Pomodorough")
        self.tray_menu = QMenu(self)
        show_action = QAction(self.strings.text("tray.show"), self)
        show_action.triggered.connect(self._show_window)
        self.tray_primary = QAction(
            self.strings.text(
                "tray.primary.start",
                phase=self.strings.text(f"phase.{self._selected_phase()}"),
            ),
            self,
        )
        self.tray_primary.triggered.connect(self._primary_action)
        quit_action = QAction(self.strings.text("tray.quit"), self)
        quit_action.triggered.connect(self._quit)
        self.tray_menu.addAction(show_action)
        self.tray_menu.addAction(self.tray_primary)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(quit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: self._show_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray.show()

    def _update_tray_progress(self, progress: float, active: bool) -> None:
        if not self.tray:
            return
        state = (active, round(max(0.0, min(1.0, progress)) * 100))
        if state == self._tray_progress_state:
            return
        self._tray_progress_state = state
        if not active:
            self.tray.setIcon(self.app_icon)
            return

        pixmap = self.app_icon.pixmap(QSize(64, 64))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        arc_rect = QRectF(5, 5, 54, 54)
        palette = self.palette()
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawEllipse(arc_rect)
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(arc_rect, 90 * 16, -round(state[1] / 100 * 360 * 16))
        painter.end()
        self.tray.setIcon(QIcon(pixmap))

    def _connect_cloud(self) -> None:
        self.cloud.signed_in.connect(self._signed_in)
        self.cloud.signed_out.connect(self._signed_out)
        self.cloud.session_expired.connect(self._session_expired)
        self.cloud.sync_ready.connect(self._apply_sync)
        self.cloud.bootstrap_ready.connect(self._bootstrap_ready)
        self.cloud.bootstrap_resolved.connect(self._apply_resolution)
        self.cloud.bootstrap_conflict.connect(self._bootstrap_conflict)
        self.cloud.revision_available.connect(self._remote_revision_available)
        self.cloud.authorization_stale.connect(self._sync)
        self.cloud.failure.connect(self._cloud_failure)
        self.cloud.account_deleted.connect(self._account_deleted)
        self.cloud.account_deletion_failed.connect(self._account_deletion_failed)

    def _connect_iroh(self) -> None:
        if self.iroh is None:
            return
        self.iroh.status_changed.connect(self._iroh_status_changed)
        self.iroh.details_changed.connect(self._iroh_details_changed)
        self.iroh.invite_ready.connect(self._iroh_invite_ready)
        self.iroh.joined.connect(self._iroh_joined)
        self.iroh.projection_changed.connect(self._iroh_projection_changed)
        self.iroh.failure.connect(self._iroh_failure)

    def _restore_replication(self) -> None:
        if self._closed:
            return
        if self.replication_mode == "centralized":
            self.cloud.restore()
            return
        if self.replication_mode != "iroh":
            self._render_network()
            return
        room_id = self.store.active_iroh_room_id
        if self.iroh is None or room_id is None:
            self._iroh_status = self.strings.text("network.unavailable")
            self._iroh_failure(
                self.strings.text("network.iroh_saved_unavailable")
            )
            return
        available, reason = self.iroh.availability()
        if not available:
            self._iroh_status = self.strings.text("network.unavailable")
            self.statusBar().showMessage(reason)
            self._render_network()
            return
        self.iroh.start_room(room_id)

    def _render_network(self) -> None:
        if not hasattr(self, "network_status"):
            return
        status = {
            "offline": self.strings.text("network.status.on_device"),
            "centralized": self.strings.text("network.status.cloud_route"),
        }.get(self.replication_mode, self._iroh_status)
        self.network_status.setText(status)
        self.account_button.setEnabled(self.replication_mode == "centralized")
        self.delete_account_button.setEnabled(
            self.cloud.authenticated and not self.cloud.deleting_account
        )
        if self.replication_mode != "centralized":
            self.account_button.setToolTip(
                self.strings.text("network.account_inactive")
            )
            self.account_button.setAccessibleName(
                self.strings.text("network.account_inactive_accessible")
            )
        room = self.store.iroh_room()
        active = self.replication_mode == "iroh" and room is not None
        available = self.iroh is not None and self.iroh.availability()[0]
        unavailable_reason = (
            self.iroh.availability()[1]
            if self.iroh is not None and not available
            else self.strings.text("network.iroh_not_packaged")
            if self.iroh is None
            else ""
        )
        self.network_unavailable.setText(unavailable_reason)
        self.network_unavailable.setVisible(bool(unavailable_reason))
        self.iroh_first_room_guidance.setVisible(available and room is None)
        self.replication_mode_combo.setAccessibleDescription(
            self.strings.text("iroh.first_room_guidance")
            if available and room is None
            else ""
        )
        self.iroh_panel.setEnabled(available)
        self.create_room_button.setEnabled(available and not active)
        self.join_room_button.setEnabled(available and not active)
        self.refresh_invite_button.setEnabled(available and active)
        self.sync_iroh_button.setEnabled(available and active)
        self.leave_room_button.setEnabled(active)
        self.copy_invite_button.setEnabled(bool(self._iroh_invite))
        if self.invite_output.toPlainText() != self._iroh_invite:
            self.invite_output.setPlainText(self._iroh_invite)
        if room is None:
            self.network_details.setText(
                self.strings.text("network.no_room")
                if available
                else self.iroh.availability()[1]
                if self.iroh
                else self.strings.text("network.service_not_packaged")
            )
            return
        peer_count = int(self._iroh_details.get("peerCount", room["peerCount"]))
        operation_count = int(
            self._iroh_details.get("operationCount", room["operationCount"])
        )
        conflict = self._iroh_details.get("conflict", room.get("conflict"))
        self.leave_room_button.setText(
            self.strings.text(
                "network.leave_rotate_room" if conflict else "network.leave_room"
            )
        )
        self.leave_room_button.setAccessibleName(
            self.strings.text("network.leave_conflicted_accessible")
            if conflict
            else self.strings.text("network.leave_accessible")
        )
        name = room.get("roomName") or self.strings.text("network.unnamed_room")
        details = self.strings.text(
            "network.room_details",
            name=name.upper(),
            room_id=room["roomId"][:10].upper(),
            peers=peer_count,
            records=operation_count,
        )
        if conflict:
            details += self.strings.text("network.repair_required")
        self.network_details.setText(details)

    def _replication_mode_changed(self, index: int) -> None:
        mode = self.replication_mode_combo.itemData(index)
        if not isinstance(mode, str) or mode == self.replication_mode:
            return
        if mode == "iroh":
            if self.iroh is None:
                self.notice.emit(self.strings.text("network.iroh_not_packaged"))
                self._reset_replication_mode_combo()
                return
            available, reason = self.iroh.availability()
            if not available:
                self.notice.emit(reason)
                self._reset_replication_mode_combo()
                return
            if self.store.active_iroh_room_id is None:
                self._reset_replication_mode_combo()
                self._show_screen(3, sync=False)
                self.create_room_button.setFocus(
                    Qt.FocusReason.ShortcutFocusReason
                )
                self.statusBar().showMessage(
                    self.strings.text("iroh.first_room_guidance"), 10_000
                )
                return
        if self.replication_mode == "centralized" and self.cloud.busy:
            self.notice.emit(self.strings.text("network.wait_cloud"))
            self._reset_replication_mode_combo()
            return
        try:
            self.store.set_replication_mode(mode)
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            self._reset_replication_mode_combo()
            return
        previous = self.replication_mode
        self.replication_mode = mode
        if previous == "iroh" and self.iroh is not None:
            self._cloud_restore_after_iroh_stop = mode == "centralized"
            self.iroh.stop()
        if mode == "centralized" and previous != "iroh":
            self.cloud.restore()
        elif mode == "iroh" and self.iroh is not None:
            self.cloud.stop_revision_stream()
            room_id = self.store.active_iroh_room_id
            if room_id:
                self.iroh.start_room(room_id)
        else:
            self.cloud.stop_revision_stream()
        self._load_state()
        self._render()

    def _reset_replication_mode_combo(self) -> None:
        previous = self.replication_mode_combo.blockSignals(True)
        self.replication_mode_combo.setCurrentIndex(
            self.replication_mode_combo.findData(self.replication_mode)
        )
        self.replication_mode_combo.blockSignals(previous)

    def _create_iroh_room(self) -> None:
        if self.iroh is None:
            self.notice.emit(self.strings.text("network.iroh_not_packaged"))
            return
        available, reason = self.iroh.availability()
        if not available:
            self.notice.emit(reason)
            return
        if self.replication_mode == "centralized" and self.cloud.busy:
            self.notice.emit(self.strings.text("network.wait_cloud_open"))
            return
        name = self.room_name_input.text().strip() or None
        try:
            room_id = self.store.create_iroh_room(secrets.token_bytes(32), name)
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            return
        self.replication_mode = "iroh"
        self.cloud.stop_revision_stream()
        self._reset_replication_mode_combo()
        self._load_state()
        self._render()
        self.iroh.start_room(room_id, emit_invite=True)

    def _join_iroh_room(self) -> None:
        if self.iroh is None:
            self.notice.emit(self.strings.text("network.iroh_not_packaged"))
            return
        available, reason = self.iroh.availability()
        if not available:
            self.notice.emit(reason)
            return
        if self.replication_mode == "centralized" and self.cloud.busy:
            self.notice.emit(self.strings.text("network.wait_cloud_join"))
            return
        try:
            invite = parse_invite(self.invite_input.toPlainText().strip())
            self.store.prepare_iroh_join(
                invite.room_id,
                invite.room_secret,
                invite.room_name,
                invite.endpoint_id,
                invite.endpoint_ticket,
            )
        except (IrohProtocolError, OSError, ValueError) as error:
            self.notice.emit(str(error))
            return
        self._iroh_status = self.strings.text("network.status.joining_room")
        self._iroh_join_pending = True
        self.cloud.stop_revision_stream()
        self._render_network()
        self.iroh.join_room(invite)

    def _leave_iroh_room(self) -> None:
        answer = QMessageBox.warning(
            self,
            self.strings.text("network.leave_title"),
            self.strings.text("network.leave_detail"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.leave_iroh_room()
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            return
        if self.iroh is not None:
            self.iroh.stop()
        self.replication_mode = "offline"
        self._iroh_invite = ""
        self._reset_replication_mode_combo()
        self._load_state()
        self._render()

    def _refresh_iroh_invite(self) -> None:
        if self.iroh is not None:
            self.iroh.refresh_invite()

    def _sync_iroh_now(self) -> None:
        if self.iroh is not None:
            try:
                self.store.capture_local_iroh_records()
            except (OSError, ValueError) as error:
                self.notice.emit(str(error))
                return
            self.iroh.sync_now()

    def _copy_iroh_invite(self) -> None:
        if self._iroh_invite:
            QApplication.clipboard().setText(self._iroh_invite)
            self.statusBar().showMessage(
                self.strings.text("network.invite_copied"), 5000
            )

    def _iroh_status_changed(self, status: str) -> None:
        self._iroh_status = status
        if status == "NOT CONNECTED" and self._cloud_restore_after_iroh_stop:
            self._cloud_restore_after_iroh_stop = False
            self.cloud.restore()
        self._render_network()

    def _iroh_details_changed(self, details: dict[str, Any]) -> None:
        self._iroh_details = details if isinstance(details, dict) else {}
        self._render_network()

    def _iroh_invite_ready(self, invite: str) -> None:
        self._iroh_invite = invite
        self._render_network()

    def _iroh_joined(self) -> None:
        self._iroh_join_pending = False
        self.replication_mode = "iroh"
        self._reset_replication_mode_combo()
        self.invite_input.clear()
        self._load_state()
        self._render()

    def _iroh_projection_changed(self) -> None:
        if self.replication_mode != "iroh":
            return
        self._load_state()
        self._render()

    def _iroh_failure(self, message: str) -> None:
        join_failed = self._iroh_join_pending
        self._iroh_join_pending = False
        self.statusBar().showMessage(message, 15000)
        if join_failed and self.store.replication_mode == "centralized":
            self.cloud.restore()
        self._render_network()

    @staticmethod
    def _response_timing(response: dict[str, Any]) -> dict[str, int | None]:
        timing = getattr(response, "timing", None)
        if not isinstance(timing, dict):
            return {}
        return {
            "request_physical_ms": timing.get("requestPhysicalMs"),
            "received_physical_ms": timing.get("receivedPhysicalMs"),
            "request_monotonic_ms": timing.get("requestMonotonicMs"),
            "received_monotonic_ms": timing.get("receivedMonotonicMs"),
        }

    def _selected_phase(self) -> str:
        value = self.settings.get("selectedPhase", "focus")
        return value if value in PHASES else "focus"

    def _current_timer(self) -> dict[str, Any]:
        phase = self._selected_phase()
        return self.timer or empty_timer(
            phase, int(self.settings["durationsMs"][phase])
        )

    def _render(self) -> None:
        source_timer = self._current_timer()
        status = source_timer.get("status", "idle")
        timer = timer_for_display(
            source_timer, self._selected_phase(), self.settings["durationsMs"]
        )
        now = self.store.effective_timer_now_ms(source_timer)
        elapsed = elapsed_ms(timer, now)
        if status == "cancelled":
            elapsed = 0
        planned = max(1, int(timer["plannedDurationMs"]))
        remaining = max(0, planned - elapsed)
        status_labels = {
            value: self.strings.text(f"status.rail.{value}")
            for value in (
                "idle",
                "running",
                "paused",
                "completed",
                "cancelled",
                "superseded",
            )
        }
        phase_label = self.strings.text(f"phase.{timer['phase']}")
        self.clock.set_state(
            format_remaining(remaining),
            phase_label,
            status_labels.get(status, status),
            elapsed / planned,
        )
        focus_count = completed_focus_count_for_day(self.history)
        break_progress = long_break_progress(focus_count)
        self.long_break_progress.setText(
            "●" * break_progress + "○" * (4 - break_progress)
        )
        self.long_break_progress.setAccessibleDescription(
            self.strings.text(
                "status.pomodoro_progress_value", count=break_progress
            )
        )
        self._render_network()
        active = status in ACTIVE_STATUSES
        self._render_task_selector(timer, active)
        self._update_tray_progress(elapsed / planned, active)
        self.primary_button.setText(
            self.strings.text("status.primary.pause")
            if status == "running"
            else self.strings.text("status.primary.resume")
            if status == "paused"
            else self.strings.text(
                "status.primary.start",
                phase=self.strings.text(f"phase.{self._selected_phase()}").upper(),
            )
        )
        mutations_enabled = not self._history_resolution_active
        self.primary_button.setEnabled(
            mutations_enabled
            and status in {"idle"} | ACTIVE_STATUSES | TERMINAL_STATUSES
        )
        self.finish_button.setEnabled(mutations_enabled and active)
        self.cancel_button.setEnabled(mutations_enabled and active)
        self.stop_sound_button.setVisible(self._sound_active)
        self.stop_sound_button.setEnabled(self._sound_active)
        for spin in self.duration_spins.values():
            spin.setEnabled(mutations_enabled)
        for phase, button in self.phase_buttons.items():
            button.setChecked(phase == self._selected_phase())
            button.setEnabled(mutations_enabled)
        self.auto_breaks.setEnabled(mutations_enabled)
        self.pattern_scope.setText(
            self.strings.text("pattern.next_scope") if active else ""
        )
        if self.tray:
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
                    phase=self.strings.text(
                        f"phase.{self._selected_phase()}"
                    ).lower(),
                )
            )
            self.tray_primary.setEnabled(self.primary_button.isEnabled())
        self._render_history()
        self._render_tasks()
        if (
            status == "completed"
            and source_timer.get("id") not in self.provisional_auto_break_timer_ids
            and source_timer.get("id") != self._notified_timer_id
        ):
            self._notified_timer_id = source_timer.get("id")
            self._notify(
                self.strings.text("status.service_arrived"),
                self.strings.text(
                    "status.service_completed",
                    phase=self.strings.text(f"phase.{source_timer['phase']}"),
                ),
            )

    def _render_history(self) -> None:
        retained = [
            item for item in self.history if item.get("status") in TERMINAL_STATUSES
        ]
        displayed = min(8, len(retained))
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
        self.history_list.clear()
        for item in retained[:8]:
            phase_value = str(item.get("phase", "timer"))
            phase = (
                self.strings.text(f"phase.{phase_value}")
                if f"phase.{phase_value}" in self.strings.messages
                else phase_value
            )
            task_id = item.get("taskId")
            task = self.known_tasks.get(task_id) if task_id else None
            task_label = (
                task["title"]
                if task
                else self.strings.text("task.deleted")
                if task_id
                else self.strings.text("task.unassigned")
            )
            status = str(item.get("status"))
            status_label = self.strings.text(f"arrivals.status.{status}")
            minutes = int(item.get("plannedDurationMs", 0)) // 60_000
            when = item.get("completedAt") or item.get("endedAt")
            try:
                local_time = datetime.fromisoformat(
                    when.replace("Z", "+00:00")
                ).astimezone()
                time_label = local_time.strftime("%a %H:%M")
            except (AttributeError, ValueError):
                time_label = self.strings.text("arrivals.time_pending")
            pending = (
                self.strings.text("arrivals.pending") if item.get("pending") else ""
            )
            text = self.strings.text(
                "arrivals.row",
                phase=phase,
                status=status_label,
                task=task_label,
                minutes=minutes,
                when=time_label,
                pending=pending,
            )
            row = QListWidgetItem(text, self.history_list)
            row.setData(Qt.ItemDataRole.AccessibleTextRole, text.replace("\n", ", "))
        if not retained:
            empty_text = self.strings.text("arrivals.empty")
            empty_item = QListWidgetItem(empty_text, self.history_list)
            empty_item.setData(
                Qt.ItemDataRole.AccessibleTextRole, empty_text.replace("\n", ", ")
            )
            empty_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _render_task_selector(self, timer: dict[str, Any], active: bool) -> None:
        selected_task_id = (
            self.settings.get("selectedTaskId")
            if self._selected_phase() == "focus"
            else None
        )
        active_task_id = timer.get("taskId") if active else None
        active_task = self.known_tasks.get(active_task_id) if active_task_id else None
        active_task_label = (
            active_task["title"]
            if active_task
            else self.strings.text("task.deleted")
            if active_task_id
            else self.strings.text("task.unassigned")
        )
        self.active_task_context.setText(
            self.strings.text("task.active_context", task=active_task_label)
            if active
            else ""
        )
        self.active_task_context.setVisible(active)
        choices = list(self.tasks)
        if selected_task_id and not any(task["id"] == selected_task_id for task in choices):
            task = self.known_tasks.get(selected_task_id)
            choices.append(
                task
                or {
                    "id": selected_task_id,
                    "title": self.strings.text("task.deleted"),
                }
            )

        signature = (
            timer.get("status"),
            self._selected_phase(),
            selected_task_id,
            self._history_resolution_active,
            tuple((task["id"], task["title"]) for task in choices),
        )
        if signature == self._task_selector_signature:
            return
        self._task_selector_signature = signature

        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem(self.strings.text("task.unassigned"), None)
        self.task_combo.setItemData(
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            Qt.ItemDataRole.TextAlignmentRole,
        )
        selected_index = 0
        for task in choices:
            self.task_combo.addItem(task["title"], task["id"])
            self.task_combo.setItemData(
                self.task_combo.count() - 1,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                Qt.ItemDataRole.TextAlignmentRole,
            )
            if task["id"] == selected_task_id:
                selected_index = self.task_combo.count() - 1
        self.task_combo.setCurrentIndex(selected_index)
        self.task_combo.blockSignals(False)
        self.task_combo.setEnabled(
            not self._history_resolution_active
            and self._selected_phase() == "focus"
        )
        self.task_combo.setAccessibleName(
            self.strings.text("task.next_focus")
            if active
            else self.strings.text("task.focus")
        )
        description = (
            self.strings.text(
                "task.next_description", task=active_task_label
            )
            if active
            else ""
        )
        self.task_combo.setAccessibleDescription(description)
        self.task_combo.setToolTip(description)

    def _render_tasks(self) -> None:
        signature = (
            datetime.now().astimezone().date(),
            self._history_resolution_active,
            tuple((task["id"], task["title"]) for task in self.tasks),
            tuple(
                (
                    item.get("id"),
                    item.get("taskId"),
                    item.get("phase"),
                    item.get("status"),
                    item.get("plannedDurationMs"),
                    item.get("completedAt") or item.get("endedAt"),
                )
                for item in self.history
            ),
        )
        if signature == self._task_render_signature:
            return
        self._task_render_signature = signature
        summaries = task_summaries_today(self.tasks, self.history)
        total_finished = sum(summary["finished"] for summary in summaries.values())
        total_ms = sum(summary["timeMs"] for summary in summaries.values())
        self.task_totals.setText(
            self.strings.text(
                "task.totals",
                count=total_finished,
                unit=self.strings.plural("task.pomodoro", total_finished),
                minutes=self._format_task_time(total_ms).upper(),
            )
        )
        self.task_input.setEnabled(not self._history_resolution_active)
        self.add_task_button.setEnabled(not self._history_resolution_active)
        self.task_table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            summary = summaries[task["id"]]
            self.task_table.setItem(row, 0, QTableWidgetItem(task["title"]))
            count = QTableWidgetItem(str(summary["finished"]))
            count.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_table.setItem(row, 1, count)
            spent = QTableWidgetItem(self._format_task_time(summary["timeMs"]))
            spent.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_table.setItem(row, 2, spent)
            delete = QPushButton(self.strings.text("task.delete"))
            delete.setObjectName("dangerButton")
            delete.setAccessibleName(
                self.strings.text("task.delete_accessible", task=task["title"])
            )
            delete.setEnabled(not self._history_resolution_active)
            delete.clicked.connect(
                lambda checked=False, task_id=task["id"]: self._delete_task(task_id)
            )
            self.task_table.setCellWidget(row, 3, delete)
        self.task_table.setVisible(bool(self.tasks))
        self.tasks_empty.setVisible(not self.tasks)

    def _format_task_time(self, milliseconds: int) -> str:
        minutes = max(0, milliseconds) // 60_000
        hours, remaining = divmod(minutes, 60)
        if hours and remaining:
            return self.strings.text(
                "duration.hours_minutes", hours=hours, minutes=remaining
            )
        if hours:
            return self.strings.text("duration.hours", hours=hours)
        return self.strings.text("duration.minutes", minutes=remaining)

    def _tick(self) -> None:
        if self._closed:
            return
        if (
            (self.user is None or not self.cloud.authenticated)
            and not self.cloud.busy
            and self.store.has_pending_auto_break()
        ):
            self._maybe_auto_start_break(require_canonical=False)
        timer = self._current_timer()
        if timer.get("status") == "running":
            if self._history_resolution_active:
                self._render()
                return
            if elapsed_ms(
                timer, self.store.effective_timer_now_ms(timer)
            ) >= int(timer["plannedDurationMs"]):
                if not self._auto_finish_in_progress:
                    self._auto_finish_in_progress = True
                    if self.replication_mode == "iroh":
                        try:
                            self.store.project_iroh_expiry()
                        except (OSError, ValueError) as error:
                            self.notice.emit(str(error))
                        self._auto_finish_in_progress = False
                        self._load_state()
                        self._render()
                        self._sync()
                    else:
                        if self.store.owns_timer(timer):
                            self._issue("finish", automatic=True)
                        else:
                            self._auto_finish_in_progress = False
                            self._render()
            else:
                self._render()

    def _mutation_blocked(self) -> bool:
        if self._iroh_join_pending:
            self.notice.emit(self.strings.text("network.wait_join"))
            return True
        if (
            not self._history_resolution_active
            and self.store.pending_resolution() is not None
        ):
            self._activate_persisted_resolution()
            self._render()
            self._set_account_state(False)
        if not self._history_resolution_active:
            return False
        self.notice.emit(
            self.strings.text("resolution.blocked")
        )
        return True

    def _primary_action(self) -> None:
        status = self._current_timer().get("status")
        if status == "running":
            self._issue("pause")
        elif status == "paused":
            self._issue("resume")
        elif status == "idle":
            self._issue("start")
        elif status in TERMINAL_STATUSES:
            if self._mutation_blocked():
                return
            try:
                self.store.queue_restart(
                    self.timer,
                    self._selected_phase(),
                    self.settings["durationsMs"],
                    self.settings.get("selectedTaskId"),
                )
            except (OSError, ValueError) as error:
                self.notice.emit(str(error))
                return
            self._load_state()
            self._render()
            self._sync()

    def _issue(self, command_type: str, automatic: bool = False) -> None:
        if self._mutation_blocked():
            self._auto_finish_in_progress = False
            return
        timer = self._current_timer()
        status = timer.get("status")
        valid = {
            "start": status == "idle",
            "pause": status == "running",
            "resume": status == "paused",
            "finish": status in ACTIVE_STATUSES,
            "cancel": status in ACTIVE_STATUSES,
            "clear": status in TERMINAL_STATUSES,
        }
        if not valid.get(command_type, False):
            self._auto_finish_in_progress = False
            return
        try:
            if command_type == "cancel":
                self.store.queue_cancel_and_clear(
                    self.timer,
                    self._selected_phase(),
                    self.settings["durationsMs"],
                    self.settings.get("selectedTaskId"),
                )
            else:
                self.store.queue_command(
                    command_type,
                    self.timer,
                    self._selected_phase(),
                    self.settings["durationsMs"],
                    self.settings.get("selectedTaskId"),
                )
        except (OSError, ValueError) as error:
            self._auto_finish_in_progress = False
            self.notice.emit(str(error))
            return
        self._load_state()
        self._render()
        self._sync()
        if command_type == "finish":
            self._auto_finish_in_progress = False
            if self.timer and self.timer.get("phase") == "focus":
                self._auto_break_not_before = time.monotonic() + 1.2
                QTimer.singleShot(1200, self._maybe_auto_start_break)
        if automatic:
            self._show_window()

    def _maybe_auto_start_break(
        self,
        *,
        sync: bool = True,
        allow_busy: bool = False,
        require_canonical: bool | None = None,
    ) -> bool:
        if (
            self._closed
            or time.monotonic() < self._auto_break_not_before
            or self._history_resolution_active
            or (self.cloud.busy and not allow_busy)
        ):
            return False
        try:
            commands = self.store.process_auto_break(
                require_canonical=(
                    self.user is not None and self.cloud.authenticated
                    if require_canonical is None
                    else require_canonical
                )
            )
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            return False
        if not commands:
            return False
        self._load_state()
        self._render()
        if sync:
            self._sync()
        return True

    def _select_phase(self, phase: str) -> None:
        if self._mutation_blocked():
            self._render()
            return
        self.settings["selectedPhase"] = phase
        self.store.set_selected_phase(phase)
        self._task_selector_signature = None
        self._render()

    def _task_selection_changed(self, index: int) -> None:
        if self._mutation_blocked():
            self._task_selector_signature = None
            self._render_task_selector(
                self._current_timer(),
                self._current_timer().get("status") in ACTIVE_STATUSES,
            )
            return
        task_id = self.task_combo.itemData(index)
        if task_id and not any(task["id"] == task_id for task in self.tasks):
            task_id = None
        try:
            self.store.set_selected_task_id(task_id)
        except (OSError, ValueError) as error:
            self._load_state()
            self._task_selector_signature = None
            self._render()
            self.notice.emit(str(error))
            return
        self._load_state()
        self._task_selector_signature = None
        self._render()
        self._sync()

    def _add_task(self) -> None:
        if self._mutation_blocked():
            return
        try:
            task = task_from_title(self.task_input.text())
            if not any(existing["id"] == task["id"] for existing in self.tasks):
                self.store.queue_task_operation("upsert", task)
            self.settings["selectedTaskId"] = task["id"]
            self.store.set_selected_task_id(task["id"])
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            return
        self.task_input.clear()
        self._load_state()
        self._render()
        self._sync()

    def _delete_task(self, task_id: str) -> None:
        if self._mutation_blocked():
            return
        task = self.known_tasks.get(task_id)
        if not task:
            return
        try:
            self.store.queue_task_operation("delete", task)
            if self.settings.get("selectedTaskId") == task_id:
                self.settings["selectedTaskId"] = None
                self.store.set_selected_task_id(None)
        except (OSError, ValueError) as error:
            self.notice.emit(str(error))
            return
        self._load_state()
        self._render()
        self._sync()

    def _duration_changed(self, phase: str, value: int) -> None:
        if self._mutation_blocked():
            self._refresh_duration_spins()
            return
        try:
            self.store.queue_duration_operation(phase, value * 60_000)
        except (OSError, ValueError) as error:
            self._load_state()
            self.notice.emit(str(error))
            return
        self._load_state()
        if not self.timer and phase == self._selected_phase():
            self._render()
        self._sync()

    def _refresh_duration_spins(self) -> None:
        for phase, spin in self.duration_spins.items():
            previous = spin.blockSignals(True)
            spin.setValue(int(self.settings["durations"][phase]))
            spin.blockSignals(previous)

    def _auto_breaks_changed(self, enabled: bool) -> None:
        if self._mutation_blocked():
            previous = self.auto_breaks.blockSignals(True)
            self.auto_breaks.setChecked(bool(self.settings.get("autoStartBreaks")))
            self.auto_breaks.blockSignals(previous)
            return
        try:
            self.store.set_auto_start_breaks(enabled)
        except (OSError, ValueError) as error:
            self._load_state()
            previous = self.auto_breaks.blockSignals(True)
            self.auto_breaks.setChecked(bool(self.settings.get("autoStartBreaks")))
            self.auto_breaks.blockSignals(previous)
            self.notice.emit(str(error))
            return
        self._load_state()
        self._sync()

    def _sync(self) -> None:
        if self._closed:
            return
        if self._iroh_join_pending:
            return
        if self.replication_mode == "iroh":
            try:
                changed = self.store.capture_local_iroh_records()
            except (OSError, ValueError) as error:
                self._iroh_failure(str(error))
                return
            if changed:
                self._load_state()
                self._render()
            if self.iroh is not None:
                self.iroh.sync_now()
            return
        if self.replication_mode != "centralized":
            return
        if (
            not self._history_resolution_active
            and self.store.pending_resolution() is not None
        ):
            self._activate_persisted_resolution()
            self._render()
            self._set_account_state(False)
        if not self.cloud.authenticated:
            return
        if self._history_resolution_active:
            self._continue_history_resolution()
            return
        payload = self.store.sync_payload()
        has_pending = bool(
            payload["commands"]
            or payload["taskOperations"]
            or payload["durationOperations"]
            or payload["autoStartOperations"]
            or payload["selectedTaskOperations"]
        )
        if has_pending:
            self._set_account_state(False)
        if self.cloud.busy:
            self._sync_when_available()
            return
        self._sync_request = payload
        self.cloud.sync(payload)

    def _sync_when_available(self) -> None:
        if self._sync_waiting:
            return
        self._sync_waiting = True
        QTimer.singleShot(100, self._retry_sync)

    def _retry_sync(self) -> None:
        if self.cloud.busy:
            QTimer.singleShot(100, self._retry_sync)
            return
        self._sync_waiting = False
        self._sync()

    def _schedule_resolution_retry(self) -> None:
        if self._resolution_retry_scheduled:
            return
        self._resolution_retry_scheduled = True
        QTimer.singleShot(100, self._retry_history_resolution)

    def _retry_history_resolution(self) -> None:
        self._resolution_retry_scheduled = False
        self._continue_history_resolution()

    def _resume_history_resolution(self) -> None:
        if not self._history_resolution_active:
            return
        self._resolution_retry_paused = False
        if self._resolution_phase == "choice" and self._resolution_preview is not None:
            self._bootstrap_ready(self._resolution_preview)
            return
        self._continue_history_resolution()

    def _continue_history_resolution(self) -> None:
        if (
            not self._history_resolution_active
            or self._resolution_retry_paused
            or not self.cloud.authenticated
            or self._resolution_user is None
            or self._account_switch_user is not None
        ):
            return
        if self.cloud.busy:
            self._schedule_resolution_retry()
            return
        if self._resolution_phase == "resolve":
            pending = self.store.pending_resolution(
                str(self._resolution_user.get("id", ""))
            )
            if pending is None:
                self._resolution_phase = "preview"
            else:
                request = pending["request"]
                self._resolution_request_id = request.get("requestId")
                self.cloud.resolve_bootstrap(request)
                return
        if self._resolution_phase == "preview":
            self.cloud.preview_bootstrap()

    def _bootstrap_ready(self, response: dict[str, Any]) -> None:
        if not self._history_resolution_active or self._resolution_user is None:
            return
        self._resolution_phase = "choice"
        try:
            plan = self.store.bootstrap_resolution_plan(
                response,
                **self._response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._resolution_phase = "preview"
            self._resolution_preview = None
            self._resolution_retry_paused = True
            self.notice.emit(str(error))
            return
        self._resolution_preview = response
        strategy = plan["strategy"]
        if strategy is None:
            strategy = self._prompt_history_resolution()
            if strategy is None or not self._confirm_history_resolution(strategy):
                self._resolution_retry_paused = True
                return
        try:
            self.store.prepare_resolution(
                self._resolution_user,
                int(plan["expectedRevision"]),
                strategy,
            )
        except (KeyError, TypeError, ValueError) as error:
            self._resolution_retry_paused = True
            self.notice.emit(str(error))
            return
        self._resolution_phase = "resolve"
        self._continue_history_resolution()

    def _prompt_history_resolution(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(self.strings.text("resolution.title"))
        dialog.setText(self.strings.text("resolution.question"))
        dialog.setInformativeText(
            self.strings.text("resolution.detail")
        )
        keep_local = dialog.addButton(
            self.strings.text("resolution.keep_local"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        keep_remote = dialog.addButton(
            self.strings.text("resolution.keep_remote"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        keep_both = dialog.addButton(
            self.strings.text("resolution.keep_both"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return {
            keep_local: "replace_remote",
            keep_remote: "keep_remote",
            keep_both: "merge",
        }.get(dialog.clickedButton())

    def _confirm_history_resolution(self, strategy: str) -> bool:
        messages = {
            "replace_remote": (
                self.strings.text("resolution.confirm_local"),
                self.strings.text("resolution.confirm_local_detail"),
            ),
            "keep_remote": (
                self.strings.text("resolution.confirm_remote"),
                self.strings.text("resolution.confirm_remote_detail"),
            ),
            "merge": (
                self.strings.text("resolution.confirm_both"),
                self.strings.text("resolution.confirm_both_detail"),
            ),
        }
        title, message = messages[strategy]
        answer = QMessageBox.warning(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _remote_revision_available(self, revision: int) -> None:
        if revision > self.revision:
            self._sync()

    def _apply_sync(self, response: dict[str, Any]) -> None:
        request = self._sync_request
        self._sync_request = None
        if request is None:
            self._set_account_state(False)
            self.notice.emit(self.strings.text("resolution.stale_response"))
            return
        try:
            notices = self.store.apply_sync(
                response,
                request,
                **self._response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._cloud_failure(str(error))
            self.notice.emit(str(error))
            return
        self._load_state()
        self._render()
        self._maybe_auto_start_break(sync=False, allow_busy=True)
        has_pending = self.store.has_sendable_sync_operations()
        self._set_account_state(not has_pending)
        if has_pending:
            self._sync()
        if notices:
            self.notice.emit(
                self.strings.text(
                    "resolution.sync_conflict", detail="; ".join(notices)
                )
            )

    def _apply_resolution(self, response: dict[str, Any]) -> None:
        if not self._history_resolution_active or self._resolution_user is None:
            return
        try:
            notices = self.store.apply_resolution(
                response,
                self._resolution_user,
                self._resolution_request_id,
                **self._response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._resolution_retry_paused = True
            self.notice.emit(str(error))
            return
        self._history_resolution_active = False
        self._resolution_phase = None
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        self._resolution_user = None
        self._load_state()
        self._render()
        self._maybe_auto_start_break(sync=False, allow_busy=True)
        has_pending = self.store.has_sendable_sync_operations()
        self._set_account_state(not has_pending)
        if has_pending:
            self._sync()
        if notices:
            self.notice.emit(
                self.strings.text(
                    "resolution.history_conflict", detail="; ".join(notices)
                )
            )

    def _bootstrap_conflict(self, details: dict[str, Any]) -> None:
        if not self._history_resolution_active or self._resolution_user is None:
            return
        user_id = self._resolution_user.get("id")
        request_id = self._resolution_request_id
        if (
            not isinstance(user_id, str)
            or not isinstance(request_id, str)
            or not self.store.discard_pending_resolution(user_id, request_id)
        ):
            self._resolution_retry_paused = True
            self.notice.emit(self.strings.text("resolution.discard_failed"))
            return
        self._resolution_phase = "preview"
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        message = details.get("message") if isinstance(details, dict) else None
        self.statusBar().showMessage(
            (message or self.strings.text("resolution.changed"))
            + self.strings.text("resolution.refreshing"),
            10_000,
        )
        self._continue_history_resolution()

    def _signed_in(self, user: dict[str, Any]) -> None:
        try:
            pending = self.store.pending_resolution()
        except ValueError as error:
            self._resolution_corruption = str(error)
            self._history_resolution_active = True
            self._resolution_user = self.user or user
            self._resolution_phase = None
            self._resolution_preview = None
            self._resolution_request_id = None
            self._resolution_retry_paused = True
            self._sync_request = None
            self._render()
            self._set_account_state(False)
            self.notice.emit(str(error))
            return
        owner = pending["owner"] if pending is not None else self.user
        owner_id = owner.get("id") if isinstance(owner, dict) else None
        if owner_id and owner_id != user.get("id"):
            self._account_switch_user = user
            self._history_resolution_active = True
            self._resolution_user = owner
            self._resolution_phase = "resolve" if pending is not None else None
            self._resolution_preview = None
            self._resolution_request_id = None
            self._resolution_retry_paused = True
            self._sync_request = None
            self._render()
            self._set_account_state(False)
            return

        self._account_switch_user = None
        if self.user is not None:
            self.store.set_user(user)
            self._load_state()
            self._history_resolution_active = False
            self._resolution_user = None
            self._resolution_phase = None
            self._resolution_preview = None
            self._resolution_request_id = None
            self._resolution_retry_paused = False
            self._render()
            self._set_account_state(False)
            self._sync()
            return

        self._history_resolution_active = True
        self._resolution_user = user
        self._resolution_phase = "resolve" if pending is not None else "preview"
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        self._render()
        self._set_account_state(False)
        self._continue_history_resolution()

    def _signed_out(self) -> None:
        if self._account_switch_user is None:
            self.store.reset_account_data()
        self._account_switch_user = None
        self._history_resolution_active = False
        self._resolution_user = None
        self._resolution_phase = None
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        self._sync_request = None
        self._load_state()
        self._activate_persisted_resolution()
        self._set_account_state(False)
        self._render()

    def _session_expired(self) -> None:
        self._account_switch_user = None
        self._history_resolution_active = False
        self._resolution_user = None
        self._resolution_phase = None
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        self._sync_request = None
        self._load_state()
        self._activate_persisted_resolution()
        self._set_account_state(False)
        self._render()
        self._schedule_pending_auto_break()

    def _set_account_state(self, synced: bool) -> None:
        self._account_synced = synced and self.cloud.authenticated
        authenticated = self.cloud.authenticated
        if self.account_button.property("authenticated") != authenticated:
            self.account_button.setProperty("authenticated", authenticated)
            self.account_button.style().unpolish(self.account_button)
            self.account_button.style().polish(self.account_button)
            self.account_button.updateGeometry()
        if not authenticated:
            self.account_button.setText(self.strings.text("account.sign_in"))
            self.account_button.setToolTip(
                self.strings.text("account.sign_in_google")
            )
            self.account_button.setAccessibleName(
                self.strings.text("account.sign_in_google")
            )
        elif self._account_switch_user is not None:
            self.account_button.setText("!")
            self.account_button.setToolTip(
                self.strings.text("account.switch_tooltip")
            )
            self.account_button.setAccessibleName(
                self.strings.text("account.switch_required")
            )
        elif self._history_resolution_active:
            self.account_button.setText("!")
            self.account_button.setToolTip(
                self.strings.text("account.resolution_tooltip")
            )
            self.account_button.setAccessibleName(
                self.strings.text("account.resolution_required")
            )
        elif self._account_synced:
            self.account_button.setText("✓")
            self.account_button.setToolTip(
                self.strings.text("account.synced_tooltip")
            )
            self.account_button.setAccessibleName(
                self.strings.text("account.synced")
            )
        else:
            self.account_button.setText("…")
            self.account_button.setToolTip(
                self.strings.text("account.pending_tooltip")
            )
            self.account_button.setAccessibleName(
                self.strings.text("account.pending")
            )

    def _open_privacy_policy(self) -> None:
        QDesktopServices.openUrl(QUrl(PRIVACY_POLICY_URL))

    def _delete_account_action(self) -> None:
        if not self.cloud.authenticated or self.cloud.deleting_account:
            return
        confirmation, accepted = QInputDialog.getText(
            self,
            self.strings.text("account.delete_prompt_title"),
            self.strings.text("account.delete_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not accepted:
            return
        if confirmation != "DELETE":
            self.statusBar().showMessage(
                self.strings.text("account.delete_mismatch"), 10_000
            )
            return
        self.cloud.delete_account(confirmation)
        self._render_network()

    def _account_deleted(self) -> None:
        self._signed_out()
        self.statusBar().showMessage(
            self.strings.text("account.delete_succeeded"), 10_000
        )

    def _account_deletion_failed(self, error: str) -> None:
        # The server did not confirm deletion; account-bound local state must
        # remain untouched and the authenticated controls become usable again.
        self._render()
        self.statusBar().showMessage(
            self.strings.text("account.delete_failed", error=error), 15_000
        )

    def _account_action(self) -> None:
        if self.cloud.authenticated:
            if self._account_switch_user is not None:
                action = self._choose_account_switch_action()
                if action == "switch":
                    user = self._account_switch_user
                    self.store.reset_account_data()
                    self._account_switch_user = None
                    self._load_state()
                    self._signed_in(user)
                elif action == "sign_out":
                    self.cloud.logout()
                return
            if self._history_resolution_active:
                action = self._choose_resolution_account_action()
                if action == "continue":
                    self._resume_history_resolution()
                elif action == "sign_out":
                    self.cloud.logout()
                return
            state = self.store.load()
            queued_changes = sum(
                len(state[key])
                for key in (
                    "pending",
                    "pendingTasks",
                    "pendingDurations",
                    "pendingAutoStarts",
                    "pendingSelectedTasks",
                )
            )
            queue_label = self.strings.plural("queue.changes", queued_changes)
            answer = QMessageBox.question(
                self,
                self.strings.text("account.signout_title"),
                self.strings.text("account.signout_detail", queue=queue_label),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.cloud.logout()
        else:
            self.cloud.login()

    def _choose_resolution_account_action(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(self.strings.text("account.resolve_title"))
        dialog.setText(self.strings.text("account.resolve_question"))
        dialog.setInformativeText(
            self.strings.text("account.resolve_detail")
        )
        resume = dialog.addButton(
            self.strings.text("account.continue_resolution"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        sign_out = dialog.addButton(
            self.strings.text("account.sign_out"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return {resume: "continue", sign_out: "sign_out"}.get(dialog.clickedButton())

    def _choose_account_switch_action(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(self.strings.text("account.different_title"))
        dialog.setText(self.strings.text("account.different_question"))
        dialog.setInformativeText(
            self.strings.text("account.different_detail")
        )
        switch = dialog.addButton(
            self.strings.text("account.clear_switch"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        sign_out = dialog.addButton(
            self.strings.text("account.sign_out"), QMessageBox.ButtonRole.RejectRole
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return {switch: "switch", sign_out: "sign_out"}.get(dialog.clickedButton())

    def _cloud_failure(self, message: str) -> None:
        self._sync_request = None
        if self.cloud.authenticated:
            self._set_account_state(False)
        self._schedule_pending_auto_break(require_canonical=False)
        if "Sign in to sync" not in message:
            self.statusBar().showMessage(message, 10_000)

    def _schedule_pending_auto_break(
        self, *, require_canonical: bool | None = None
    ) -> None:
        if self.store.has_pending_auto_break():
            delay_ms = max(
                0,
                math.ceil(
                    (self._auto_break_not_before - time.monotonic()) * 1000
                ),
            )
            QTimer.singleShot(
                delay_ms,
                lambda: self._maybe_auto_start_break(
                    allow_busy=True, require_canonical=require_canonical
                ),
            )

    def _show_notice(self, message: str) -> None:
        QMessageBox.warning(self, "Pomodorough", message)

    def _notify(self, title: str, message: str) -> None:
        QApplication.beep()
        self._sound_active = True
        self.sound_timer.start()
        self.stop_sound_button.setVisible(True)
        self.stop_sound_button.setEnabled(True)
        if self.tray:
            self.tray.showMessage(title, message, self.app_icon, 7000)

    def _stop_sound(self) -> None:
        self.sound_timer.stop()
        self._sound_active = False
        self.stop_sound_button.setVisible(False)
        self.stop_sound_button.setEnabled(False)

    def _stop_sound_and_clear(self) -> None:
        status = self._current_timer().get("status")
        self._stop_sound()
        if status in TERMINAL_STATUSES:
            self._issue("clear")

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._stop_sound()
        self.quitting = True
        QApplication.quit()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.tick_timer.stop()
        self.sync_timer.stop()
        self._stop_sound()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray and not self.quitting:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                self.strings.text("tray.running_title"),
                self.strings.text("tray.running_detail"),
                self.app_icon,
                3500,
            )
        else:
            self.shutdown()
            event.accept()

    @staticmethod
    def _stylesheet() -> str:
        return """
        QWidget#root { background: palette(window); color: palette(window-text); font-family: "Noto Sans", "Segoe UI", sans-serif; font-size: 12px; }
        QLabel#brand { color: palette(window-text); font-family: "DejaVu Sans Condensed"; font-size: 24px; font-weight: 900; letter-spacing: 2px; }
        QLabel#tagline, QLabel#device { color: palette(window-text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 2px; }
        QFrame#rule { color: palette(highlight); background: palette(highlight); max-height: 4px; min-height: 4px; border: 0; }
        QFrame#ticket { background: palette(base); color: palette(text); border: 3px solid palette(highlight); }
        QLabel#sectionTitle { color: palette(text); font-family: "DejaVu Sans Condensed"; font-size: 14px; font-weight: 900; letter-spacing: 1px; }
        QLabel#countBadge { background: palette(highlight); color: palette(highlighted-text); border: 2px solid palette(mid); padding: 2px 8px; font-weight: bold; }
        QPushButton { min-height: 29px; background: palette(button); color: palette(text); border: 2px solid palette(text); padding: 3px 7px; font-weight: 800; }
        QPushButton:hover { border-color: palette(highlight); }
        QPushButton:pressed { padding-top: 5px; padding-left: 9px; }
        QPushButton:focus { border: 3px solid palette(highlight); }
        QPushButton:disabled { color: palette(text); border-color: palette(text); background: palette(button); }
        QPushButton#primaryButton { background: palette(highlight); color: palette(highlighted-text); min-height: 36px; font-size: 14px; }
        QPushButton#accountButton { border-color: palette(highlight); min-width: 72px; max-width: 150px; }
        QPushButton#accountButton[authenticated="true"] { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0; }
        QPushButton[phase="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QPushButton[screen="true"] { min-width: 64px; font-family: "DejaVu Sans Condensed"; letter-spacing: 1px; }
        QPushButton[screen="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QToolButton#settingsButton { font-size: 24px; }
        QFrame#taskSelector { background: palette(base); border: 2px solid palette(mid); }
        QFrame#networkPanel { background: palette(base); border: 2px solid palette(mid); }
        QLabel#microLabel { color: palette(text); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QLabel#taskSubtitle { color: palette(mid); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QLabel#emptyState { color: palette(mid); padding: 24px; }
        QComboBox, QLineEdit, QPlainTextEdit { min-height: 29px; background: palette(base); color: palette(text); border: 2px solid palette(mid); padding: 2px 6px; }
        QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus { border: 3px solid palette(highlight); }
        QLabel#privacyNotice { color: palette(mid); background: palette(alternate-base); border-left: 4px solid palette(highlight); padding: 8px; font-family: "DejaVu Sans Mono"; font-size: 9px; }
        QTableWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); gridline-color: palette(alternate-base); outline: none; }
        QHeaderView::section { background: palette(button); color: palette(button-text); border: 1px solid palette(mid); padding: 6px; font-weight: 800; }
        QSpinBox { min-width: 68px; font-family: "DejaVu Sans Mono"; }
        QCheckBox { spacing: 8px; }
        QListWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); outline: none; padding: 3px; }
        QListWidget::item { border-bottom: 1px solid palette(alternate-base); padding: 7px 5px; }
        QStatusBar { background: palette(window); color: palette(window-text); }
        """
