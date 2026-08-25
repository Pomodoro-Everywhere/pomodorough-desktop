from __future__ import annotations

import time  # noqa: F401 - public test seam patches shared time module
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from .localization import Strings
from .network import CloudService
from .sound import CompletionSound
from .storage import Store
from .ui_controller import (
    WindowApplicationController,
    presented_timer as presented_timer,
)
from .ui_views import PRIVACY_POLICY_URL, MainWindowViewMixin


def resource_path(name: str) -> Path:
    return Path(__file__).parent / "resources" / name


class MainWindow(MainWindowViewMixin, WindowApplicationController, QMainWindow):
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
        self._initialize_services(store, cloud, app_icon, iroh, locale)
        self._initialize_runtime_state()
        self._load_state()
        self._activate_persisted_resolution()
        self._build_window()
        self._connect_cloud()
        self._connect_iroh()
        self._render()
        self._start_background_timers()

    def _initialize_services(
        self,
        store: Store,
        cloud: CloudService,
        app_icon: QIcon,
        iroh: Any | None,
        locale: str | None,
    ) -> None:
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

    def _initialize_runtime_state(self) -> None:
        self._iroh_details: dict[str, Any] = {}
        self._iroh_invite = ""
        self._cloud_restore_after_iroh_stop = False
        self._iroh_join_pending = False
        self.quitting = False
        self._closed = False
        self._notified_timer_id: str | None = None
        self._sound_active = False
        self._alert_timer_identity: tuple[object, object, object] | None = None
        self.completion_sound = CompletionSound()
        self.sound_timer = QTimer(self)
        self.sound_timer.setInterval(1_200)
        self.sound_timer.timeout.connect(self.completion_sound.play)
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

    def _build_window(self) -> None:
        self.setWindowTitle(self.strings.text("window.title"))
        self.setWindowIcon(self.app_icon)
        self.setMinimumSize(600, 340)
        self.resize(640, 360)
        self._build_ui()
        self._build_tray()

    def _start_background_timers(self) -> None:
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(250)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self._sync)
        self.sync_timer.start(15_000)
        QTimer.singleShot(0, self._restore_replication)

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
            lambda reason: (
                self._show_window()
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
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
        painter.setPen(
            QPen(
                palette.color(QPalette.ColorRole.Mid),
                8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.FlatCap,
            )
        )
        painter.drawEllipse(arc_rect)
        painter.setPen(
            QPen(
                palette.color(QPalette.ColorRole.Highlight),
                8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.FlatCap,
            )
        )
        painter.drawArc(arc_rect, 90 * 16, -round(state[1] / 100 * 360 * 16))
        painter.end()
        self.tray.setIcon(QIcon(pixmap))

    def _open_privacy_policy(self) -> None:
        QDesktopServices.openUrl(QUrl(PRIVACY_POLICY_URL))

    def _show_notice(self, message: str) -> None:
        QMessageBox.warning(self, "Pomodorough", message)

    def _notify(self, title: str, message: str) -> None:
        source_timer = self._current_timer()
        self._alert_timer_identity = (
            source_timer.get("id"),
            source_timer.get("phase"),
            source_timer.get("status"),
        )
        self.completion_sound.play()
        self._sound_active = True
        self.sound_timer.start()
        self.stop_sound_button.setVisible(True)
        self.stop_sound_button.setEnabled(True)
        if self.tray:
            self.tray.showMessage(title, message, self.app_icon, 7000)

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
