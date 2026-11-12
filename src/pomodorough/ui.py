from __future__ import annotations

import time  # noqa: F401 - public test seam patches shared time module
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QIcon,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from .controller_outcomes import ControllerOutcome, EmitFailure, EmitTaskFailure
from .failure_notice import describe_failure
from .localization import Strings
from .network import CloudService
from .railway_palette import STEEL, TRACK
from .sound import CompletionSound
from .storage import Store
from .task_failure_notice import ask_to_retry, describe_task_failure
from .tray_progress_icon import TrayProgressIcon
from .ui_controller import (
    WindowApplicationController,
    presented_timer as presented_timer,
)
from .ui_views import PRIVACY_POLICY_URL, MainWindowViewMixin


def resource_path(name: str) -> Path:
    return Path(__file__).parent / "resources" / name


def apply_railway_highlight(app: QApplication) -> None:
    """Pin the selection colors so native widgets stop leaking OS blue.

    Only Highlight and HighlightedText get a resolve bit, so OS theme
    changes keep updating every other role. Steel on track stays legible
    on both light and dark bases, unlike navy platform on dark.
    """
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor(STEEL))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TRACK))
    app.setPalette(palette)


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
        self._replay_pending_task_delete()
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
        self._notice_dialog_active = False
        # L1: task failure sentences that arrived while another dialog was
        # open, with their Retry action when the change can repeat.
        self._waiting_failure_sentences: list[
            tuple[str, Callable[[], ControllerOutcome[Any]] | None]
        ] = []

    def _build_window(self) -> None:
        self.setWindowTitle(self.strings.text("window.title"))
        self.setWindowIcon(self.app_icon)
        self.setMinimumSize(600, 340)
        self.resize(640, 360)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_railway_highlight(app)
        self._build_ui()
        self._fit_default_width_to_navigation()
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
            self._schedule_nav_reflow()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        if not hasattr(self, "clock"):
            return
        event_type = event.type()
        if event_type == QEvent.Type.PaletteChange:
            self._refresh_stylesheet()
            self._tray_progress_state = None
            self.clock.update()
            if hasattr(self, "tray"):
                self._render()
        elif event_type in (
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
        ):
            self._refresh_font_dependent_content()
        elif event_type == QEvent.Type.LayoutDirectionChange:
            self._schedule_nav_reflow()

    def _refresh_font_dependent_content(self) -> None:
        previous_scale = getattr(self, "_font_scale", None)
        self._refresh_stylesheet()
        if getattr(self, "_font_scale", None) == previous_scale:
            return
        self.clock.update()
        self.updateGeometry()
        self._schedule_nav_reflow()
        schedule = getattr(
            getattr(self, "tasks_screen", None), "_schedule_row_resize", None
        )
        if callable(schedule):
            schedule()
        self._refresh_invite_minimums()

    def _refresh_invite_minimums(self) -> None:
        for name in ("invite_input", "invite_output"):
            field = getattr(self, name, None)
            if field is None:
                continue
            try:
                frame = field.frameWidth() * 2
                margin = int(field.document().documentMargin() * 2)
                field.setMinimumHeight(
                    field.fontMetrics().lineSpacing() * 3 + frame + margin
                )
            except RuntimeError:
                continue

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self._tray_progress_icon = TrayProgressIcon(resource_path("icon.svg"))
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
                self._show_tray_menu()
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
        )
        self.tray.show()

    def _show_tray_menu(self) -> None:
        if self.tray_menu:
            self.tray_menu.popup(QCursor.pos())

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
        self.tray.setIcon(self._tray_progress_icon.icon(state[1] / 100))

    def _open_privacy_policy(self) -> None:
        QDesktopServices.openUrl(QUrl(PRIVACY_POLICY_URL))

    def _show_notice(self, message: str) -> None:
        # R50-D02: the 250 ms tick keeps firing inside the modal nested
        # loop, so a continuing read failure would stack dialogs.
        # Keep at most one warning active; suppressed repeats are still
        # bounded upstream by the pending-break notice incident.
        if self._notice_dialog_active:
            return
        self._notice_dialog_active = True
        try:
            # A standard OK button keeps Qt's English label in every locale.
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Pomodorough")
            dialog.setText(message)
            dialog.addButton(
                self.strings.text("notice.ok"), QMessageBox.ButtonRole.AcceptRole
            )
            dialog.exec()
        finally:
            self._notice_dialog_active = False
        self._show_waiting_failure_sentence()

    def _show_task_failure(self, failure: EmitTaskFailure) -> None:
        if self._hold_unattended_task_delete_failure(failure):
            # M3: no Retry dialog on a hiding, quitting or hidden window.
            return
        notice = describe_task_failure(self.strings, failure.change, failure.error)
        retry = failure.retry if notice.can_retry else None
        if self._notice_dialog_active:
            # L1: a task failure is never dropped; it waits for the open dialog.
            self._wait_for_open_dialog(notice.sentence, retry)
            return
        self._show_failure_sentence(notice.sentence, retry)

    def _show_failure(self, failure: EmitFailure) -> None:
        notice = describe_failure(self.strings, failure.error)
        self._show_failure_sentence(
            notice.sentence, failure.retry if notice.can_retry else None
        )

    def _show_failure_sentence(
        self, sentence: str, retry: Callable[[], ControllerOutcome[Any]] | None
    ) -> None:
        """Show one failure sentence; offer Retry when the action can repeat."""
        if retry is None:
            self.notice.emit(sentence)
            return
        if self._notice_dialog_active:
            self._wait_for_open_dialog(sentence, retry)
            return
        self._notice_dialog_active = True
        try:
            retry_chosen = ask_to_retry(self, self.strings, sentence)
        finally:
            self._notice_dialog_active = False
        if retry_chosen:
            self._apply_outcome(retry())
        self._show_waiting_failure_sentence()

    def _wait_for_open_dialog(
        self, sentence: str, retry: Callable[[], ControllerOutcome[Any]] | None
    ) -> None:
        # L1: one dialog at a time. This sentence waits for the open one to
        # close; a repeat of a waiting sentence adds nothing.
        waiting = self._waiting_failure_sentences
        if all(sentence != queued for queued, _retry in waiting):
            waiting.append((sentence, retry))

    def _show_waiting_failure_sentence(self) -> None:
        if self._waiting_failure_sentences:
            sentence, retry = self._waiting_failure_sentences.pop(0)
            self._show_failure_sentence(sentence, retry)

    def _is_window_foreground(self) -> bool:
        if self.isActiveWindow():
            return True
        return QApplication.activeWindow() is self

    def _notify(self, title: str, message: str) -> None:
        source_timer = self._current_timer()
        identity = (
            source_timer.get("id"),
            source_timer.get("phase"),
            source_timer.get("status"),
        )
        if self._sound_active and self._alert_timer_identity == identity:
            return
        self._alert_timer_identity = identity
        self.completion_sound.play()
        self._sound_active = True
        self.sound_timer.start()
        self.stop_sound_button.setVisible(True)
        self.stop_sound_button.setEnabled(True)
        if self.tray and not self._is_window_foreground():
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
        # A deferred task delete must reach the store before it closes. A
        # failure opens no dialog here; the durable marker makes the next
        # start ask the user about the delete.
        self.complete_pending_task_delete_unattended()
        # M2: the last chance in this run to clear the marker of a delete
        # that ended, so the next start cannot replay it.
        self._apply_outcome(
            self._owned_controller("timer").retry_withdrawn_task_delete_clear()
        )
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
