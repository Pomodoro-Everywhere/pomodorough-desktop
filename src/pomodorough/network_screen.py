from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .localization import Strings

PRIVACY_POLICY_URL = "https://pomodorough.egigoka.me/privacy"


class NetworkScreen(QFrame):
    replication_mode_requested = Signal(str)
    privacy_policy_requested = Signal()
    delete_account_requested = Signal()
    create_room_requested = Signal(str)
    join_room_requested = Signal(str)
    copy_invite_requested = Signal()
    refresh_invite_requested = Signal()
    sync_now_requested = Signal()
    leave_room_requested = Signal()

    def __init__(self, strings: Strings, replication_mode: str) -> None:
        super().__init__()
        self.strings = strings
        self.setObjectName("ticket")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_mode_row(replication_mode))
        layout.addLayout(self._build_account_row())
        self._build_notices(layout)
        layout.addWidget(self._build_iroh_panel())
        self._build_footer(layout)

    def _build_header(self) -> QHBoxLayout:
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
        self.network_status.setAccessibleName(
            self.strings.text("network.status_accessible")
        )
        header.addWidget(self.network_status)
        return header

    def _build_mode_row(self, replication_mode: str) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(self.strings.text("network.route"))
        label.setObjectName("microLabel")
        self.replication_mode_combo = QComboBox()
        self.replication_mode_combo.setAccessibleName(
            self.strings.text("network.mode_accessible")
        )
        for mode in ("offline", "iroh", "centralized"):
            self.replication_mode_combo.addItem(
                self.strings.text(f"network.mode.{mode}"), mode
            )
        self.set_replication_mode(replication_mode)
        self.replication_mode_combo.currentIndexChanged.connect(
            self._request_replication_mode
        )
        row.addWidget(label)
        row.addWidget(self.replication_mode_combo, 1)
        return row

    def _request_replication_mode(self, index: int) -> None:
        mode = self.replication_mode_combo.itemData(index)
        if isinstance(mode, str):
            self.replication_mode_requested.emit(mode)

    def set_replication_mode(self, mode: str) -> None:
        combo = getattr(self, "replication_mode_combo", None)
        if combo is None:
            return
        previous = combo.blockSignals(True)
        combo.setCurrentIndex(max(0, combo.findData(mode)))
        combo.blockSignals(previous)

    def _build_account_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(self.strings.text("account.heading"))
        label.setObjectName("microLabel")
        self.privacy_policy_button = QPushButton(
            self.strings.text("account.privacy_policy")
        )
        self.privacy_policy_button.setAccessibleDescription(PRIVACY_POLICY_URL)
        self.privacy_policy_button.clicked.connect(self.privacy_policy_requested)
        self.delete_account_button = QPushButton(self.strings.text("account.delete"))
        self.delete_account_button.setObjectName("dangerButton")
        self.delete_account_button.clicked.connect(self.delete_account_requested)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(self.privacy_policy_button)
        row.addWidget(self.delete_account_button)
        return row

    def _build_notices(self, layout: QVBoxLayout) -> None:
        self.network_unavailable = QLabel("")
        self.network_unavailable.setObjectName("privacyNotice")
        self.network_unavailable.setWordWrap(True)
        self.network_unavailable.setAccessibleName(
            self.strings.text("network.iroh_unavailable_accessible")
        )
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

    def _build_iroh_panel(self) -> QFrame:
        self.iroh_panel = QFrame()
        self.iroh_panel.setObjectName("networkPanel")
        layout = QGridLayout(self.iroh_panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._build_create_row(layout)
        self._build_join_row(layout)
        self._build_invite_row(layout)
        self._build_actions(layout)
        return self.iroh_panel

    def _build_create_row(self, layout: QGridLayout) -> None:
        self.room_name_input = QLineEdit()
        self.room_name_input.setPlaceholderText(
            self.strings.text("network.room_name_placeholder")
        )
        self.room_name_input.setMaxLength(64)
        self.room_name_input.setAccessibleName(
            self.strings.text("network.room_name_accessible")
        )
        self.create_room_button = QPushButton(self.strings.text("network.create_room"))
        self.create_room_button.setObjectName("primaryButton")
        self.create_room_button.setAccessibleName(
            self.strings.text("network.create_room_accessible")
        )
        self.create_room_button.clicked.connect(
            lambda: self.create_room_requested.emit(self.room_name_input.text())
        )
        layout.addWidget(self.room_name_input, 0, 0)
        layout.addWidget(self.create_room_button, 0, 1)

    def _build_join_row(self, layout: QGridLayout) -> None:
        self.invite_input = QPlainTextEdit()
        self.invite_input.setPlaceholderText(
            self.strings.text("network.invite_placeholder")
        )
        self.invite_input.setAccessibleName(
            self.strings.text("network.invite_accessible")
        )
        self.invite_input.setMaximumHeight(70)
        self.join_room_button = QPushButton(self.strings.text("network.join_room"))
        self.join_room_button.setAccessibleName(
            self.strings.text("network.join_room_accessible")
        )
        self.join_room_button.clicked.connect(
            lambda: self.join_room_requested.emit(self.invite_input.toPlainText())
        )
        layout.addWidget(self.invite_input, 1, 0)
        layout.addWidget(self.join_room_button, 1, 1)

    def _build_invite_row(self, layout: QGridLayout) -> None:
        self.invite_output = QPlainTextEdit()
        self.invite_output.setReadOnly(True)
        self.invite_output.setPlaceholderText(
            self.strings.text("network.invite_output_placeholder")
        )
        self.invite_output.setAccessibleName(
            self.strings.text("network.invite_output_accessible")
        )
        self.invite_output.setMaximumHeight(70)
        self.copy_invite_button = QPushButton(self.strings.text("network.copy_invite"))
        self.copy_invite_button.setAccessibleName(
            self.strings.text("network.copy_invite_accessible")
        )
        self.copy_invite_button.clicked.connect(self.copy_invite_requested)
        layout.addWidget(self.invite_output, 2, 0)
        layout.addWidget(self.copy_invite_button, 2, 1)

    def _build_actions(self, layout: QGridLayout) -> None:
        row = QHBoxLayout()
        self.refresh_invite_button = QPushButton(
            self.strings.text("network.refresh_ticket")
        )
        self.refresh_invite_button.clicked.connect(self.refresh_invite_requested)
        self.sync_iroh_button = QPushButton(self.strings.text("network.sync_now"))
        self.sync_iroh_button.clicked.connect(self.sync_now_requested)
        self.leave_room_button = QPushButton(self.strings.text("network.leave_room"))
        self.leave_room_button.setObjectName("dangerButton")
        self.leave_room_button.clicked.connect(self.leave_room_requested)
        row.addWidget(self.refresh_invite_button)
        row.addWidget(self.sync_iroh_button)
        row.addStretch()
        row.addWidget(self.leave_room_button)
        layout.addLayout(row, 3, 0, 1, 2)

    def _build_footer(self, layout: QVBoxLayout) -> None:
        self.network_details = QLabel(self.strings.text("network.no_room"))
        self.network_details.setObjectName("device")
        self.network_details.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.network_details.setAccessibleName(
            self.strings.text("network.details_accessible")
        )
        layout.addWidget(self.network_details)
        privacy = QLabel(self.strings.text("network.privacy"))
        privacy.setObjectName("privacyNotice")
        privacy.setWordWrap(True)
        privacy.setAccessibleName(self.strings.text("network.privacy_accessible"))
        layout.addWidget(privacy)
        layout.addStretch()

    def render(
        self,
        *,
        replication_mode: str,
        iroh_status: str,
        iroh_details: dict[str, Any],
        invite: str,
        room: dict[str, Any] | None,
        available: bool,
        unavailable_reason: str,
        cloud_authenticated: bool,
        cloud_deleting_account: bool,
    ) -> None:
        self.set_replication_mode(replication_mode)
        status = {
            "offline": self.strings.text("network.status.on_device"),
            "centralized": self.strings.text("network.status.cloud_route"),
        }.get(replication_mode, iroh_status)
        self.network_status.setText(status)
        self.delete_account_button.setEnabled(
            cloud_authenticated and not cloud_deleting_account
        )
        self._render_controls(
            replication_mode,
            room,
            available,
            unavailable_reason,
            invite,
        )
        if room is None:
            self._render_without_room(available, unavailable_reason)
        else:
            self._render_room(room, iroh_details)

    def _render_controls(
        self,
        replication_mode: str,
        room: dict[str, Any] | None,
        available: bool,
        unavailable_reason: str,
        invite: str,
    ) -> None:
        active = replication_mode == "iroh" and room is not None
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
        self.copy_invite_button.setEnabled(bool(invite))
        if self.invite_output.toPlainText() != invite:
            self.invite_output.setPlainText(invite)

    def _render_without_room(self, available: bool, unavailable_reason: str) -> None:
        self.network_details.setText(
            self.strings.text("network.no_room")
            if available
            else unavailable_reason or self.strings.text("network.service_not_packaged")
        )

    def _render_room(
        self,
        room: dict[str, Any],
        iroh_details: dict[str, Any],
    ) -> None:
        peer_count = int(iroh_details.get("peerCount", room["peerCount"]))
        operation_count = int(
            iroh_details.get("operationCount", room["operationCount"])
        )
        conflict = iroh_details.get("conflict", room.get("conflict"))
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

    @staticmethod
    def stylesheet() -> str:
        return """
        QFrame#networkPanel { background: palette(base); border: 2px solid palette(mid); }
        QLabel#privacyNotice { color: palette(mid); background: palette(alternate-base); border-left: 4px solid palette(highlight); padding: 8px; font-family: "DejaVu Sans Mono"; font-size: 9px; }
        """
