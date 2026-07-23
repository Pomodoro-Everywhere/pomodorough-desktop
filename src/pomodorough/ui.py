from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
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
    elapsed_ms,
    empty_timer,
    format_remaining,
    rebuild_optimistic,
    rebuild_tasks,
    task_from_title,
    task_summaries_today,
)
from .network import CloudService
from .storage import Store


def resource_path(name: str) -> Path:
    return Path(__file__).parent / "resources" / name


class ClockWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.time_text = "25:00"
        self.phase_text = "FOCUS"
        self.status_text = "READY AT PLATFORM"
        self.progress = 0.0
        self.setMinimumSize(170, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(360, 360)

    def set_state(self, time_text: str, phase: str, status: str, progress: float) -> None:
        self.time_text = time_text
        self.phase_text = phase.upper()
        self.status_text = status.upper()
        self.progress = max(0.0, min(1.0, progress))
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

    def __init__(self, store: Store, cloud: CloudService, app_icon: QIcon) -> None:
        super().__init__()
        self.store = store
        self.cloud = cloud
        self.app_icon = app_icon
        self.quitting = False
        self._notified_timer_id: str | None = None
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
        self._account_switch_user: dict[str, Any] | None = None
        self._task_selector_signature: tuple[Any, ...] | None = None
        self._task_render_signature: tuple[Any, ...] | None = None
        self._load_state()
        self._activate_persisted_resolution()
        self.setWindowTitle("Pomodorough — Time, in transit")
        self.setWindowIcon(app_icon)
        self.setMinimumSize(600, 340)
        self.resize(640, 360)
        self._build_ui()
        self._build_tray()
        self._connect_cloud()
        self._render()

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(250)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self._sync)
        self.sync_timer.start(15_000)
        QTimer.singleShot(0, self.cloud.restore)

    def _load_state(self) -> None:
        previous_timer = getattr(self, "timer", None)
        state, provisional_timer_ids = (
            self.store.load_with_provisional_auto_breaks()
        )
        pending_resolution = self.store.pending_resolution()
        self.settings = state["settings"]
        self.revision = int(state["snapshot"].get("revision", 0))
        self.base_timer = state["snapshot"].get("canonicalTimer")
        self.base_history = state["snapshot"].get("history", [])
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
            self.base_timer, self.base_history, self.pending
        )
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
        selected_task_id = self.settings.get("selectedTaskId")
        if selected_task_id and not any(task["id"] == selected_task_id for task in self.tasks):
            self.settings["selectedTaskId"] = None
            if pending_resolution is None:
                self.store.set_selected_task_id(None)
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
        pending = self.store.pending_resolution()
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
        title = QLabel("POMODOROUGH")
        title.setObjectName("brand")
        tagline = QLabel("TIME, IN TRANSIT")
        tagline.setObjectName("tagline")
        brand.addWidget(title)
        brand.addWidget(tagline)
        header.addLayout(brand)
        self.screen_group = QButtonGroup(self)
        self.screen_group.setExclusive(True)
        self.screen_buttons: list[QPushButton] = []
        for index, label in enumerate(("TIMER", "TASKS", "ARRIVALS")):
            button = QPushButton(label)
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
        self.settings_button.setIconSize(QSize(20, 20))
        settings_icon = QIcon.fromTheme("settings-configure")
        if settings_icon.isNull():
            self.settings_button.setText("⚙")
            self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        else:
            self.settings_button.setIcon(settings_icon)
            self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.settings_button.setToolTip("Show settings")
        self.settings_button.setAccessibleName("Show settings")
        self.settings_button.toggled.connect(self._settings_toggled)
        self.account_button = QPushButton("SIGN IN")
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
        self.clock = ClockWidget()
        self.left_layout.addWidget(self.clock, 1)

        self.actions_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.actions_layout.setSpacing(8)
        self.task_selector_panel = QFrame()
        self.task_selector_panel.setObjectName("taskSelector")
        self.task_selector_panel.setMinimumWidth(190)
        self.task_selector_panel.setMaximumHeight(76)
        task_selector_layout = QVBoxLayout(self.task_selector_panel)
        task_selector_layout.setContentsMargins(8, 5, 8, 7)
        task_selector_layout.setSpacing(3)
        task_selector_label = QLabel("FOCUS TASK")
        task_selector_label.setObjectName("microLabel")
        self.task_combo = QComboBox()
        self.task_combo.setAccessibleName("Focus task")
        self.task_combo.currentIndexChanged.connect(self._task_selection_changed)
        task_selector_layout.addWidget(task_selector_label)
        task_selector_layout.addWidget(self.task_combo)
        self.primary_button = QPushButton("START")
        self.primary_button.setObjectName("primaryButton")
        self.primary_button.clicked.connect(self._primary_action)
        self.finish_button = QPushButton("FINISH")
        self.finish_button.clicked.connect(lambda: self._issue("finish"))
        self.cancel_button = QPushButton("CANCEL")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(lambda: self._issue("cancel"))
        self.actions_layout.addWidget(self.task_selector_panel)
        self.actions_layout.addWidget(self.primary_button, 1)
        self.actions_layout.addWidget(self.finish_button, 1)
        self.actions_layout.addWidget(self.cancel_button, 1)
        self.left_layout.addLayout(self.actions_layout)
        self.content_layout.addLayout(self.left_layout, 3)

        self.right_panel = QFrame()
        self.right_panel.setObjectName("ticket")
        self.right_layout = QVBoxLayout(self.right_panel)

        service_label = QLabel("SERVICE PATTERN")
        service_label.setObjectName("sectionTitle")
        self.right_layout.addWidget(service_label)
        self.phase_group = QButtonGroup(self)
        self.phase_group.setExclusive(True)
        self.phase_buttons: dict[str, QPushButton] = {}
        phase_layout = QHBoxLayout()
        phase_layout.setSpacing(5)
        for phase, definition in PHASES.items():
            button = QPushButton(definition["label"].replace(" break", "").upper())
            button.setToolTip(definition["label"])
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
            label = QLabel(definition["label"])
            spin = QSpinBox()
            spin.setRange(1, 180)
            spin.setSuffix(" min")
            spin.setValue(int(self.settings["durations"][phase]))
            spin.valueChanged.connect(lambda value, key=phase: self._duration_changed(key, value))
            duration_grid.addWidget(label, row, 0)
            duration_grid.addWidget(spin, row, 1)
            self.duration_spins[phase] = spin
        self.right_layout.addLayout(duration_grid)
        self.auto_breaks = QCheckBox("Auto-start breaks")
        self.auto_breaks.setChecked(bool(self.settings.get("autoStartBreaks")))
        self.auto_breaks.toggled.connect(self._auto_breaks_changed)
        self.right_layout.addWidget(self.auto_breaks)
        self.right_layout.addStretch()
        self.content_layout.addWidget(self.right_panel, 2)
        self.right_panel.hide()
        self.page_stack.addWidget(self.timer_page)
        self.tasks_page = self._build_tasks_page()
        self.page_stack.addWidget(self.tasks_page)
        self.arrivals_page = self._build_arrivals_page()
        self.page_stack.addWidget(self.arrivals_page)
        self.outer_layout.addWidget(self.page_stack, 1)

        self.shortcuts = [
            QShortcut(QKeySequence(Qt.Key.Key_Space), self),
            QShortcut(QKeySequence("Ctrl+Shift+F"), self),
        ]
        self.shortcuts[0].activated.connect(self._primary_action)
        self.shortcuts[1].activated.connect(lambda: self._issue("finish"))
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
        title = QLabel("TASK BOARD")
        title.setObjectName("sectionTitle")
        subtitle = QLabel("Completed focus services, grouped for today")
        subtitle.setObjectName("taskSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.task_totals = QLabel("0 POMODOROS • 0 MIN TODAY")
        self.task_totals.setObjectName("countBadge")
        header.addWidget(self.task_totals)
        layout.addLayout(header)

        form = QHBoxLayout()
        self.task_input = QLineEdit()
        self.task_input.setPlaceholderText("Add a task")
        self.task_input.setAccessibleName("Task name")
        self.task_input.returnPressed.connect(self._add_task)
        self.add_task_button = QPushButton("ADD TASK")
        self.add_task_button.setObjectName("primaryButton")
        self.add_task_button.clicked.connect(self._add_task)
        form.addWidget(self.task_input, 1)
        form.addWidget(self.add_task_button)
        layout.addLayout(form)

        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(
            ("Task", "Finished pomodoros today", "Time today spent", "")
        )
        self.task_table.verticalHeader().hide()
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.task_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        table_header = self.task_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.task_table, 1)
        self.tasks_empty = QLabel("No tasks yet. Add one, then assign it before starting focus.")
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
        title = QLabel("RECENT ARRIVALS")
        title.setObjectName("sectionTitle")
        self.history_count = QLabel("0")
        self.history_count.setObjectName("countBadge")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.history_count)
        layout.addLayout(header)

        self.history_list = QListWidget()
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.history_list, 1)
        self.device_label = QLabel(f"DEVICE  {self.store.device_id[-8:].upper()}")
        self.device_label.setObjectName("device")
        layout.addWidget(self.device_label)
        return page

    def _refresh_stylesheet(self) -> None:
        palette_key = QApplication.palette().cacheKey()
        if palette_key == self._palette_key:
            return
        self._palette_key = palette_key
        self.setStyleSheet(self._stylesheet())

    def _settings_toggled(self, visible: bool) -> None:
        self.right_panel.setVisible(visible)
        label = "Hide settings" if visible else "Show settings"
        self.settings_button.setToolTip(label)
        self.settings_button.setAccessibleName(label)

    def _show_screen(self, index: int) -> None:
        self.page_stack.setCurrentIndex(index)
        self.screen_buttons[index].setChecked(True)
        self.settings_button.setVisible(index == 0)
        self._render()
        if index:
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
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip("Pomodorough")
        menu = QMenu()
        show_action = QAction("Show Pomodorough", self)
        show_action.triggered.connect(self._show_window)
        self.tray_primary = QAction("Start", self)
        self.tray_primary.triggered.connect(self._primary_action)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addAction(self.tray_primary)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
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

    def _selected_phase(self) -> str:
        value = self.settings.get("selectedPhase", "focus")
        return value if value in PHASES else "focus"

    def _current_timer(self) -> dict[str, Any]:
        phase = self._selected_phase()
        return self.timer or empty_timer(
            phase, int(self.settings["durationsMs"][phase])
        )

    def _render(self) -> None:
        timer = self._current_timer()
        now = int(time.time() * 1000)
        elapsed = elapsed_ms(timer, now)
        if timer.get("status") == "cancelled":
            elapsed = 0
        planned = max(1, int(timer["plannedDurationMs"]))
        remaining = max(0, planned - elapsed)
        status = timer.get("status", "idle")
        status_labels = {
            "idle": "READY AT PLATFORM",
            "running": "IN TRANSIT",
            "paused": "HELD AT SIGNAL",
            "completed": "ARRIVED",
            "cancelled": "SERVICE CANCELLED",
            "superseded": "ROUTE CHANGED",
        }
        self.clock.set_state(
            format_remaining(remaining),
            PHASES[timer["phase"]]["label"],
            status_labels.get(status, status),
            elapsed / planned,
        )
        active = status in ACTIVE_STATUSES
        self._render_task_selector(timer, active)
        self._update_tray_progress(elapsed / planned, active)
        self.primary_button.setText("PAUSE" if status == "running" else "RESUME" if status == "paused" else "START")
        mutations_enabled = not self._history_resolution_active
        self.primary_button.setEnabled(
            mutations_enabled
            and status in {"idle"} | ACTIVE_STATUSES | TERMINAL_STATUSES
        )
        self.finish_button.setEnabled(mutations_enabled and active)
        self.cancel_button.setEnabled(mutations_enabled and active)
        for spin in self.duration_spins.values():
            spin.setEnabled(mutations_enabled)
        for phase, button in self.phase_buttons.items():
            button.setChecked(phase == self._selected_phase())
            button.setEnabled(mutations_enabled and not active)
        self.auto_breaks.setEnabled(mutations_enabled)
        if self.tray:
            self.tray.setToolTip(f"Pomodorough • {format_remaining(remaining)} • {status_labels.get(status, status)}")
            self.tray_primary.setText(self.primary_button.text().title())
            self.tray_primary.setEnabled(self.primary_button.isEnabled())
        self._render_history()
        self._render_tasks()
        if (
            status == "completed"
            and timer.get("id") not in self.provisional_auto_break_timer_ids
            and timer.get("id") != self._notified_timer_id
        ):
            self._notified_timer_id = timer.get("id")
            self._notify("Service arrived", f'{PHASES[timer["phase"]]["label"]} completed.')

    def _render_history(self) -> None:
        completed = [item for item in self.history if item.get("status") == "completed"]
        self.history_count.setText(str(len(completed)))
        self.history_list.clear()
        for item in completed[:8]:
            phase = PHASES.get(item.get("phase"), {"label": item.get("phase", "Timer")})["label"]
            task = self.known_tasks.get(item.get("taskId"))
            if task:
                phase = f'{phase} · {task["title"]}'
            minutes = int(item.get("plannedDurationMs", 0)) // 60_000
            when = item.get("completedAt") or item.get("endedAt")
            try:
                local_time = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone()
                time_label = local_time.strftime("%a %H:%M")
            except (AttributeError, ValueError):
                time_label = "Pending"
            marker = "  •" if item.get("pending") else ""
            QListWidgetItem(f"{phase}  {minutes} min\n{time_label}{marker}", self.history_list)
        if not completed:
            QListWidgetItem("No arrivals yet.\nStart first service.", self.history_list)

    def _render_task_selector(self, timer: dict[str, Any], active: bool) -> None:
        running = timer.get("status") == "running"
        selected_task_id = (
            timer.get("taskId")
            if running
            else self.settings.get("selectedTaskId")
            if self._selected_phase() == "focus"
            else None
        )
        choices = list(self.tasks)
        if selected_task_id and not any(task["id"] == selected_task_id for task in choices):
            task = self.known_tasks.get(selected_task_id)
            choices.append(task or {"id": selected_task_id, "title": "Deleted task"})

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
        self.task_combo.addItem("No task", None)
        selected_index = 0
        for task in choices:
            self.task_combo.addItem(task["title"], task["id"])
            if task["id"] == selected_task_id:
                selected_index = self.task_combo.count() - 1
        self.task_combo.setCurrentIndex(selected_index)
        self.task_combo.blockSignals(False)
        self.task_combo.setEnabled(
            not self._history_resolution_active
            and not running
            and self._selected_phase() == "focus"
        )
        self.task_combo.setToolTip(
            "Select task for next focus session; paused session keeps its current task."
            if active
            else "Select task for next focus session."
        )

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
            f"{total_finished} POMODOROS • {self._format_task_time(total_ms).upper()} TODAY"
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
            delete = QPushButton("DELETE")
            delete.setObjectName("dangerButton")
            delete.setAccessibleName(f'Delete {task["title"]}')
            delete.setEnabled(not self._history_resolution_active)
            delete.clicked.connect(
                lambda checked=False, task_id=task["id"]: self._delete_task(task_id)
            )
            self.task_table.setCellWidget(row, 3, delete)
        self.task_table.setVisible(bool(self.tasks))
        self.tasks_empty.setVisible(not self.tasks)

    @staticmethod
    def _format_task_time(milliseconds: int) -> str:
        minutes = max(0, milliseconds) // 60_000
        hours, remaining = divmod(minutes, 60)
        if hours and remaining:
            return f"{hours} hr {remaining} min"
        if hours:
            return f"{hours} hr"
        return f"{remaining} min"

    def _tick(self) -> None:
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
            if elapsed_ms(timer, int(time.time() * 1000)) >= int(timer["plannedDurationMs"]):
                if not self._auto_finish_in_progress:
                    self._auto_finish_in_progress = True
                    self._issue("finish", automatic=True)
            else:
                self._render()

    def _mutation_blocked(self) -> bool:
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
            "Resolve local and synced history before changing timers, tasks, or durations."
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
            self._issue("clear")
            if self._current_timer().get("status") == "idle":
                self._issue("start")

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
            command = self.store.queue_command(
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
        self.pending.append(command)
        self.timer, self.history = rebuild_optimistic(
            self.base_timer, self.base_history, self.pending
        )
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
            time.monotonic() < self._auto_break_not_before
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
        if self._current_timer().get("status") in ACTIVE_STATUSES:
            return
        self.settings["selectedPhase"] = phase
        self.store.set_selected_phase(phase)
        if not self.timer:
            self._render()

    def _task_selection_changed(self, index: int) -> None:
        if self._mutation_blocked():
            self._task_selector_signature = None
            self._render_task_selector(
                self._current_timer(),
                self._current_timer().get("status") in ACTIVE_STATUSES,
            )
            return
        if self._current_timer().get("status") == "running":
            return
        task_id = self.task_combo.itemData(index)
        if task_id and not any(task["id"] == task_id for task in self.tasks):
            task_id = None
        self.settings["selectedTaskId"] = task_id
        self.store.set_selected_task_id(task_id)
        self._task_selector_signature = None

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
            plan = self.store.bootstrap_resolution_plan(response)
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
        dialog.setWindowTitle("Choose Pomodorough history")
        dialog.setText("Local and synced histories both contain completed timers.")
        dialog.setInformativeText("Choose which history should become canonical.")
        keep_local = dialog.addButton(
            "Keep Local", QMessageBox.ButtonRole.AcceptRole
        )
        keep_remote = dialog.addButton(
            "Keep Remote", QMessageBox.ButtonRole.AcceptRole
        )
        keep_both = dialog.addButton("Keep Both", QMessageBox.ButtonRole.AcceptRole)
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
                "Replace synced history?",
                "Keep Local permanently replaces synced timer history with this device's history.",
            ),
            "keep_remote": (
                "Discard local history?",
                "Keep Remote permanently discards this device's local timer history and pending changes.",
            ),
            "merge": (
                "Combine both histories?",
                "Keep Both can produce timer conflicts or sync errors. Continue only if you accept that risk.",
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
            self.notice.emit("Sync response did not match an active request.")
            return
        try:
            notices = self.store.apply_sync(response, request)
        except (KeyError, TypeError, ValueError) as error:
            self._cloud_failure(str(error))
            self.notice.emit(str(error))
            return
        self._load_state()
        self._render()
        self._maybe_auto_start_break(sync=False, allow_busy=True)
        payload = self.store.sync_payload()
        has_pending = bool(
            payload["commands"]
            or payload["taskOperations"]
            or payload["durationOperations"]
            or payload["autoStartOperations"]
        )
        self._set_account_state(not has_pending)
        if has_pending:
            self._sync()
        if notices:
            self.notice.emit("Server resolved a sync conflict: " + "; ".join(notices))

    def _apply_resolution(self, response: dict[str, Any]) -> None:
        if not self._history_resolution_active or self._resolution_user is None:
            return
        try:
            notices = self.store.apply_resolution(
                response,
                self._resolution_user,
                self._resolution_request_id,
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
        payload = self.store.sync_payload()
        has_pending = bool(
            payload["commands"]
            or payload["taskOperations"]
            or payload["durationOperations"]
            or payload["autoStartOperations"]
        )
        self._set_account_state(not has_pending)
        if has_pending:
            self._sync()
        if notices:
            self.notice.emit(
                "Server resolved a history conflict: " + "; ".join(notices)
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
            self.notice.emit("Could not discard stale history resolution request.")
            return
        self._resolution_phase = "preview"
        self._resolution_preview = None
        self._resolution_request_id = None
        self._resolution_retry_paused = False
        message = details.get("message") if isinstance(details, dict) else None
        self.statusBar().showMessage(
            (message or "History changed before resolution could be applied.")
            + " Refreshing synced history; local data is preserved.",
            10_000,
        )
        self._continue_history_resolution()

    def _signed_in(self, user: dict[str, Any]) -> None:
        pending = self.store.pending_resolution()
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
            self.account_button.setText("SIGN IN")
            self.account_button.setToolTip("Sign in with Google")
            self.account_button.setAccessibleName("Sign in with Google")
        elif self._account_switch_user is not None:
            self.account_button.setText("!")
            self.account_button.setToolTip(
                "Signed-in account differs from local data owner. Click to switch or sign out."
            )
            self.account_button.setAccessibleName("Account switch required")
        elif self._history_resolution_active:
            self.account_button.setText("!")
            self.account_button.setToolTip(
                "Signed in; local and synced history must be resolved. Click to continue."
            )
            self.account_button.setAccessibleName("History resolution required")
        elif self._account_synced:
            self.account_button.setText("✓")
            self.account_button.setToolTip("Signed in and synced. Click to sign out.")
            self.account_button.setAccessibleName("Signed in and synced")
        else:
            self.account_button.setText("…")
            self.account_button.setToolTip("Signed in; sync pending. Click to sign out.")
            self.account_button.setAccessibleName("Signed in; sync pending")

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
            answer = QMessageBox.question(
                self,
                "Sign out of Pomodorough?",
                "Signing out clears this account's timer, history, and tasks from this device.",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.cloud.logout()
        else:
            self.cloud.login()

    def _choose_resolution_account_action(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("History resolution required")
        dialog.setText("Continue resolving history or sign out?")
        dialog.setInformativeText(
            "Signing out clears this account's timer, history, tasks, and pending resolution from this device."
        )
        resume = dialog.addButton(
            "Continue Resolution", QMessageBox.ButtonRole.AcceptRole
        )
        sign_out = dialog.addButton("Sign Out", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return {resume: "continue", sign_out: "sign_out"}.get(dialog.clickedButton())

    def _choose_account_switch_action(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Different account detected")
        dialog.setText("Signed-in account does not own this device's local data.")
        dialog.setInformativeText(
            "Clear the previous account's local timer, history, tasks, and pending resolution to switch, or sign out without changing that data."
        )
        switch = dialog.addButton(
            "Clear Data & Switch", QMessageBox.ButtonRole.DestructiveRole
        )
        sign_out = dialog.addButton("Sign Out", QMessageBox.ButtonRole.RejectRole)
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
        if self.tray:
            self.tray.showMessage(title, message, self.app_icon, 7000)

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self.quitting = True
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray and not self.quitting:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "Pomodorough is still running",
                "Timer remains available in system tray.",
                self.app_icon,
                3500,
            )
        else:
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
        QPushButton { min-height: 29px; background: palette(button); color: palette(button-text); border: 2px solid palette(mid); padding: 3px 7px; font-weight: 800; }
        QPushButton:hover { border-color: palette(highlight); }
        QPushButton:pressed { padding-top: 5px; padding-left: 9px; }
        QPushButton:focus { border: 3px solid palette(highlight); }
        QPushButton:disabled { color: palette(mid); border-color: palette(mid); background: palette(button); }
        QPushButton#primaryButton { background: palette(highlight); color: palette(highlighted-text); min-height: 36px; font-size: 14px; }
        QPushButton#accountButton { border-color: palette(highlight); min-width: 72px; max-width: 150px; }
        QPushButton#accountButton[authenticated="true"] { min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; padding: 0; }
        QPushButton[phase="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QPushButton[screen="true"] { min-width: 64px; font-family: "DejaVu Sans Condensed"; letter-spacing: 1px; }
        QPushButton[screen="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-bottom: 5px solid palette(highlighted-text); }
        QFrame#taskSelector { background: palette(base); border: 2px solid palette(mid); }
        QLabel#microLabel, QLabel#taskSubtitle { color: palette(mid); font-family: "DejaVu Sans Mono"; font-size: 9px; letter-spacing: 1px; }
        QLabel#emptyState { color: palette(mid); padding: 24px; }
        QComboBox, QLineEdit { min-height: 29px; background: palette(base); color: palette(text); border: 2px solid palette(mid); padding: 2px 6px; }
        QComboBox:focus, QLineEdit:focus { border: 3px solid palette(highlight); }
        QTableWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); gridline-color: palette(alternate-base); outline: none; }
        QHeaderView::section { background: palette(button); color: palette(button-text); border: 1px solid palette(mid); padding: 6px; font-weight: 800; }
        QSpinBox { min-width: 68px; font-family: "DejaVu Sans Mono"; }
        QCheckBox { spacing: 8px; }
        QListWidget { background: palette(base); color: palette(text); border: 2px solid palette(mid); outline: none; padding: 3px; }
        QListWidget::item { border-bottom: 1px solid palette(alternate-base); padding: 7px 5px; }
        QStatusBar { background: palette(window); color: palette(window-text); }
        """
