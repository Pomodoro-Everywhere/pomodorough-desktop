from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from .controller_outcomes import (
    ControllerOutcome,
    EmitNotice,
    LoadState,
    Render,
    RenderNetwork,
    ShowStatus,
    done,
    returning,
)
from .iroh_protocol import IrohProtocolError, parse_invite


@dataclass(frozen=True, slots=True)
class ReplicationContext:
    store: Any
    cloud: Any
    iroh: Any | None
    strings: Any
    closed: bool


@dataclass(frozen=True, slots=True)
class ReplicationPorts:
    context: Callable[[], ReplicationContext]
    apply_outcome: Callable[[ControllerOutcome[Any]], None]
    dialog_parent: Callable[[], QWidget]
    replication_mode_data: Callable[[int], Any]
    set_replication_mode: Callable[[str], None]
    show_screen: Callable[[int, bool], None]
    focus_create_room: Callable[[Qt.FocusReason], None]
    room_name_text: Callable[[], str]
    invite_text: Callable[[], str]
    clear_invite_input: Callable[[], None]
    iroh_failure: Callable[[str], None]


class ReplicationController:
    """Owns replication route and Iroh room connection state."""

    def __init__(self, ports: ReplicationPorts) -> None:
        self._ports = ports
        self.mode = "offline"
        self.iroh_status = ""
        self.iroh_details: dict[str, Any] = {}
        self.iroh_invite = ""
        self.cloud_restore_after_iroh_stop = False
        self.iroh_join_pending = False

    def _context(self) -> ReplicationContext:
        return self._ports.context()

    def restore_replication(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.closed:
            return done()
        if self.mode == "centralized":
            context.cloud.restore()
            return done()
        if self.mode != "iroh":
            return done(RenderNetwork())
        room_id = context.store.active_iroh_room_id
        if context.iroh is None or room_id is None:
            self.iroh_status = context.strings.text("network.unavailable")
            self._ports.iroh_failure(
                context.strings.text("network.iroh_saved_unavailable")
            )
            return done()
        available, reason = context.iroh.availability()
        if not available:
            self.iroh_status = context.strings.text("network.unavailable")
            return done(ShowStatus(reason), RenderNetwork())
        context.iroh.start_room(room_id)
        return done()

    def replication_mode_changed(self, selection: int | str) -> ControllerOutcome[None]:
        mode = (
            selection
            if isinstance(selection, str)
            else self._ports.replication_mode_data(selection)
        )
        if not isinstance(mode, str) or mode == self.mode:
            return done()
        if not self._replication_transition_ready_value(mode):
            return done()
        context = self._context()
        try:
            context.store.set_replication_mode(mode)
        except (OSError, ValueError) as error:
            self._ports.set_replication_mode(self.mode)
            return done(EmitNotice(str(error)))
        previous = self.mode
        self.mode = mode
        self._activate_replication_mode_value(previous, mode)
        return done(LoadState(), Render())

    def replication_transition_ready(self, mode: str) -> ControllerOutcome[bool]:
        return returning(self._replication_transition_ready_value(mode))

    def _replication_transition_ready_value(self, mode: str) -> bool:
        context = self._context()
        if mode == "iroh" and not self._iroh_transition_ready_value():
            return False
        if self.mode == "centralized" and context.cloud.busy:
            self._ports.set_replication_mode(self.mode)
            self._ports.apply_outcome(
                done(EmitNotice(context.strings.text("network.wait_cloud")))
            )
            return False
        return True

    def iroh_transition_ready(self) -> ControllerOutcome[bool]:
        return returning(self._iroh_transition_ready_value())

    def _iroh_transition_ready_value(self) -> bool:
        context = self._context()
        if context.iroh is None:
            self._ports.set_replication_mode(self.mode)
            self._ports.apply_outcome(
                done(EmitNotice(context.strings.text("network.iroh_not_packaged")))
            )
            return False
        available, reason = context.iroh.availability()
        if not available:
            self._ports.set_replication_mode(self.mode)
            self._ports.apply_outcome(done(EmitNotice(reason)))
            return False
        if context.store.active_iroh_room_id is not None:
            return True
        self._ports.set_replication_mode(self.mode)
        self._ports.show_screen(3, False)
        self._ports.focus_create_room(Qt.FocusReason.ShortcutFocusReason)
        self._ports.apply_outcome(
            done(ShowStatus(context.strings.text("iroh.first_room_guidance"), 10_000))
        )
        return False

    def activate_replication_mode(
        self, previous: str, mode: str
    ) -> ControllerOutcome[None]:
        self._activate_replication_mode_value(previous, mode)
        return done()

    def _activate_replication_mode_value(self, previous: str, mode: str) -> None:
        context = self._context()
        if previous == "iroh" and context.iroh is not None:
            self.cloud_restore_after_iroh_stop = mode == "centralized"
            context.iroh.stop()
        if mode == "centralized" and previous != "iroh":
            context.cloud.restore()
        elif mode == "iroh" and context.iroh is not None:
            context.cloud.stop_revision_stream()
            room_id = context.store.active_iroh_room_id
            if room_id:
                context.iroh.start_room(room_id)
        else:
            context.cloud.stop_revision_stream()

    def create_iroh_room(
        self, requested_name: str | None = None
    ) -> ControllerOutcome[None]:
        context = self._context()
        if context.iroh is None:
            return done(EmitNotice(context.strings.text("network.iroh_not_packaged")))
        available, reason = context.iroh.availability()
        if not available:
            return done(EmitNotice(reason))
        if self.mode == "centralized" and context.cloud.busy:
            return done(EmitNotice(context.strings.text("network.wait_cloud_open")))
        name = (
            requested_name.strip()
            if isinstance(requested_name, str) and requested_name.strip()
            else self._ports.room_name_text().strip() or None
        )
        try:
            room_id = context.store.create_iroh_room(secrets.token_bytes(32), name)
        except (OSError, ValueError) as error:
            return done(EmitNotice(str(error)))
        self.mode = "iroh"
        context.cloud.stop_revision_stream()
        self._ports.set_replication_mode(self.mode)
        self._ports.apply_outcome(done(LoadState(), Render()))
        context.iroh.start_room(room_id, emit_invite=True)
        return done()

    def join_iroh_room(
        self, requested_invite: str | None = None
    ) -> ControllerOutcome[None]:
        context = self._context()
        if context.iroh is None:
            return done(EmitNotice(context.strings.text("network.iroh_not_packaged")))
        available, reason = context.iroh.availability()
        if not available:
            return done(EmitNotice(reason))
        if self.mode == "centralized" and context.cloud.busy:
            return done(EmitNotice(context.strings.text("network.wait_cloud_join")))
        try:
            invite_text = (
                requested_invite.strip()
                if isinstance(requested_invite, str)
                else self._ports.invite_text().strip()
            )
            invite = parse_invite(invite_text)
            context.store.prepare_iroh_join(
                invite.room_id,
                invite.room_secret,
                invite.room_name,
                invite.endpoint_id,
                invite.endpoint_ticket,
            )
        except (IrohProtocolError, OSError, ValueError) as error:
            return done(EmitNotice(str(error)))
        self.iroh_status = context.strings.text("network.status.joining_room")
        self.iroh_join_pending = True
        context.cloud.stop_revision_stream()
        self._ports.apply_outcome(done(RenderNetwork()))
        context.iroh.join_room(invite)
        return done()

    def leave_iroh_room(self) -> ControllerOutcome[None]:
        context = self._context()
        answer = QMessageBox.warning(
            self._ports.dialog_parent(),
            context.strings.text("network.leave_title"),
            context.strings.text("network.leave_detail"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return done()
        try:
            context.store.leave_iroh_room()
        except (OSError, ValueError) as error:
            return done(EmitNotice(str(error)))
        if context.iroh is not None:
            context.iroh.stop()
        self.mode = "offline"
        self.iroh_invite = ""
        self._ports.set_replication_mode(self.mode)
        return done(LoadState(), Render())

    def refresh_iroh_invite(self) -> ControllerOutcome[None]:
        iroh = self._context().iroh
        if iroh is not None:
            iroh.refresh_invite()
        return done()

    def sync_iroh_now(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.iroh is not None:
            try:
                context.store.capture_local_iroh_records()
            except (OSError, ValueError) as error:
                return done(EmitNotice(str(error)))
            context.iroh.sync_now()
        return done()

    def copy_iroh_invite(self) -> ControllerOutcome[None]:
        if not self.iroh_invite:
            return done()
        QApplication.clipboard().setText(self.iroh_invite)
        return done(
            ShowStatus(self._context().strings.text("network.invite_copied"), 5000)
        )

    def iroh_status_changed(self, status: str) -> ControllerOutcome[None]:
        self.iroh_status = status
        context = self._context()
        if status == "NOT CONNECTED" and self.cloud_restore_after_iroh_stop:
            self.cloud_restore_after_iroh_stop = False
            context.cloud.restore()
        return done(RenderNetwork())

    def iroh_details_changed(self, details: dict[str, Any]) -> ControllerOutcome[None]:
        self.iroh_details = details if isinstance(details, dict) else {}
        return done(RenderNetwork())

    def iroh_invite_ready(self, invite: str) -> ControllerOutcome[None]:
        self.iroh_invite = invite
        return done(RenderNetwork())

    def iroh_joined(self) -> ControllerOutcome[None]:
        self.iroh_join_pending = False
        self.mode = "iroh"
        self._ports.set_replication_mode(self.mode)
        self._ports.clear_invite_input()
        return done(LoadState(), Render())

    def iroh_projection_changed(self) -> ControllerOutcome[None]:
        if self.mode != "iroh":
            return done()
        return done(LoadState(), Render())

    def iroh_failure(self, message: str) -> ControllerOutcome[None]:
        context = self._context()
        join_failed = self.iroh_join_pending
        self.iroh_join_pending = False
        self._ports.apply_outcome(done(ShowStatus(message, 15_000)))
        if join_failed and context.store.replication_mode == "centralized":
            context.cloud.restore()
        return done(RenderNetwork())
