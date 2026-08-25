from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from pomodorough.controller_outcomes import (
    EmitNotice,
    LoadState,
    Render,
    RenderNetwork,
    ShowStatus,
)
from pomodorough.iroh_protocol import IrohProtocolError, RoomInvite
from pomodorough.replication_controller import (
    ReplicationContext,
    ReplicationController,
    ReplicationPorts,
)


class FakeStrings:
    @staticmethod
    def text(key: str) -> str:
        return key


class FakeCloud:
    def __init__(self) -> None:
        self.busy = False
        self.restores = 0
        self.stops = 0

    def restore(self) -> None:
        self.restores += 1

    def stop_revision_stream(self) -> None:
        self.stops += 1


class FakeStore:
    def __init__(self) -> None:
        self.active_iroh_room_id: str | None = None
        self.replication_mode = "offline"
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None

    def set_replication_mode(self, mode: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.replication_mode = mode
        self.calls.append(("set_mode", mode))

    def create_iroh_room(self, secret: bytes, name: str | None) -> str:
        if self.failure is not None:
            raise self.failure
        self.calls.append(("create", secret, name))
        self.active_iroh_room_id = "room-created"
        return "room-created"

    def prepare_iroh_join(self, *arguments: object) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(("prepare_join", *arguments))

    def leave_iroh_room(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(("leave",))
        self.active_iroh_room_id = None

    def capture_local_iroh_records(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(("capture",))


class FakeIroh:
    def __init__(self) -> None:
        self.available = True
        self.reason = "iroh unavailable"
        self.calls: list[tuple[object, ...]] = []

    def availability(self) -> tuple[bool, str]:
        return self.available, self.reason

    def start_room(self, room_id: str, *, emit_invite: bool = False) -> None:
        self.calls.append(("start", room_id, emit_invite))

    def stop(self) -> None:
        self.calls.append(("stop",))

    def join_room(self, invite: RoomInvite) -> None:
        self.calls.append(("join", invite))

    def refresh_invite(self) -> None:
        self.calls.append(("refresh",))

    def sync_now(self) -> None:
        self.calls.append(("sync",))


class SimpleClipboard:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:
        self.text = value


class SimpleApplication:
    def __init__(self, clipboard: SimpleClipboard) -> None:
        self._clipboard = clipboard

    def clipboard(self) -> SimpleClipboard:
        return self._clipboard


class ControllerHarness:
    def __init__(self, *, iroh: FakeIroh | None = None) -> None:
        self.store = FakeStore()
        self.cloud = FakeCloud()
        self.iroh = iroh
        self.closed = False
        self.applied = []
        self.mode_selections: list[str] = []
        self.screens: list[tuple[int, bool]] = []
        self.focus_reasons: list[Qt.FocusReason] = []
        self.failures: list[str] = []
        self.invite_clears = 0
        self.room_name = "  Team room  "
        self.invite = "invalid invite"
        self.ports = ReplicationPorts(
            context=self.context,
            apply_outcome=self.applied.append,
            dialog_parent=lambda: None,  # type: ignore[arg-type]
            replication_mode_data=lambda index: {0: "offline", 1: "centralized", 2: "iroh"}.get(index),
            set_replication_mode=self.mode_selections.append,
            show_screen=lambda index, force: self.screens.append((index, force)),
            focus_create_room=self.focus_reasons.append,
            room_name_text=lambda: self.room_name,
            invite_text=lambda: self.invite,
            clear_invite_input=self.clear_invite,
            iroh_failure=self.failures.append,
        )
        self.controller = ReplicationController(self.ports)

    def context(self) -> ReplicationContext:
        return ReplicationContext(
            store=self.store,
            cloud=self.cloud,
            iroh=self.iroh,
            strings=FakeStrings(),
            closed=self.closed,
        )

    def clear_invite(self) -> None:
        self.invite_clears += 1


class ReplicationControllerTests(unittest.TestCase):
    def test_restore_routes_closed_offline_cloud_and_iroh_states(self) -> None:
        harness = ControllerHarness()
        harness.closed = True
        self.assertEqual(harness.controller.restore_replication().effects, ())

        harness.closed = False
        harness.controller.mode = "offline"
        self.assertEqual(
            effect_types(harness.controller.restore_replication()),
            (RenderNetwork,),
        )

        harness.controller.mode = "centralized"
        harness.controller.restore_replication()
        self.assertEqual(harness.cloud.restores, 1)

        harness.controller.mode = "iroh"
        harness.store.active_iroh_room_id = "room-saved"
        harness.controller.restore_replication()
        self.assertEqual(harness.failures, ["network.iroh_saved_unavailable"])
        self.assertEqual(harness.controller.iroh_status, "network.unavailable")

        iroh = FakeIroh()
        harness.iroh = iroh
        iroh.available = False
        unavailable = harness.controller.restore_replication()
        self.assertEqual(effect_types(unavailable), (ShowStatus, RenderNetwork))
        self.assertEqual(unavailable.effects[0].message, "iroh unavailable")

        iroh.available = True
        harness.controller.restore_replication()
        self.assertEqual(iroh.calls[-1], ("start", "room-saved", False))

    def test_mode_transitions_enforce_packaging_busy_room_and_persistence_boundaries(self) -> None:
        harness = ControllerHarness()
        blocked = harness.controller.replication_transition_ready("iroh")
        self.assertFalse(blocked.value)
        self.assertEqual(harness.mode_selections, ["offline"])
        self.assertIsInstance(harness.applied[-1].effects[0], EmitNotice)

        iroh = FakeIroh()
        harness.iroh = iroh
        harness.controller.mode = "centralized"
        harness.cloud.busy = True
        self.assertFalse(harness.controller.replication_transition_ready("offline").value)
        self.assertEqual(harness.applied[-1].effects[0].message, "network.wait_cloud")

        harness.cloud.busy = False
        harness.controller.mode = "offline"
        self.assertFalse(harness.controller.iroh_transition_ready().value)
        self.assertEqual(harness.screens, [(3, False)])
        self.assertEqual(
            harness.focus_reasons,
            [Qt.FocusReason.ShortcutFocusReason],
        )

        harness.store.active_iroh_room_id = "room-1"
        self.assertTrue(harness.controller.iroh_transition_ready().value)
        harness.store.failure = OSError("mode write failed")
        failed = harness.controller.replication_mode_changed("iroh")
        self.assertEqual(failed.effects[0].message, "mode write failed")
        self.assertEqual(harness.controller.mode, "offline")

        harness.store.failure = None
        changed = harness.controller.replication_mode_changed(2)
        self.assertEqual(effect_types(changed), (LoadState, Render))
        self.assertEqual(harness.controller.mode, "iroh")
        self.assertEqual(harness.cloud.stops, 1)
        self.assertEqual(iroh.calls[-1], ("start", "room-1", False))

        harness.controller.activate_replication_mode("iroh", "centralized")
        self.assertTrue(harness.controller.cloud_restore_after_iroh_stop)
        self.assertEqual(iroh.calls[-1], ("stop",))
        harness.controller.iroh_status_changed("NOT CONNECTED")
        self.assertEqual(harness.cloud.restores, 1)
        self.assertFalse(harness.controller.cloud_restore_after_iroh_stop)

    def test_create_and_join_cover_success_and_fail_closed_paths(self) -> None:
        harness = ControllerHarness()
        missing = harness.controller.create_iroh_room()
        self.assertEqual(missing.effects[0].message, "network.iroh_not_packaged")

        iroh = FakeIroh()
        harness.iroh = iroh
        iroh.available = False
        unavailable = harness.controller.create_iroh_room()
        self.assertEqual(unavailable.effects[0].message, "iroh unavailable")

        iroh.available = True
        harness.controller.mode = "centralized"
        harness.cloud.busy = True
        busy = harness.controller.create_iroh_room()
        self.assertEqual(busy.effects[0].message, "network.wait_cloud_open")

        harness.cloud.busy = False
        with patch("pomodorough.replication_controller.secrets.token_bytes", return_value=b"s" * 32):
            created = harness.controller.create_iroh_room()
        self.assertEqual(created.effects, ())
        self.assertEqual(harness.store.calls[-1], ("create", b"s" * 32, "Team room"))
        self.assertEqual(iroh.calls[-1], ("start", "room-created", True))
        self.assertEqual(harness.controller.mode, "iroh")

        invite = RoomInvite(
            room_id="room-joined",
            endpoint_ticket="ticket",
            endpoint_id="endpoint",
            room_secret=bytes(range(32)),
            room_name="Joined room",
        )
        harness.controller.mode = "offline"
        with patch("pomodorough.replication_controller.parse_invite", return_value=invite):
            joined = harness.controller.join_iroh_room("  invitation  ")
        self.assertEqual(joined.effects, ())
        self.assertTrue(harness.controller.iroh_join_pending)
        self.assertEqual(harness.controller.iroh_status, "network.status.joining_room")
        self.assertEqual(harness.store.calls[-1][0], "prepare_join")
        self.assertEqual(iroh.calls[-1], ("join", invite))
        self.assertIsInstance(harness.applied[-1].effects[0], RenderNetwork)

        with patch(
            "pomodorough.replication_controller.parse_invite",
            side_effect=IrohProtocolError("bad invite"),
        ):
            rejected = harness.controller.join_iroh_room("bad")
        self.assertEqual(rejected.effects[0].message, "bad invite")

    def test_noop_error_and_clipboard_paths_remain_side_effect_safe(self) -> None:
        harness = ControllerHarness()
        self.assertEqual(harness.controller.replication_mode_changed("offline").effects, ())
        self.assertEqual(harness.controller.replication_mode_changed(99).effects, ())
        harness.controller.refresh_iroh_invite()
        self.assertEqual(harness.controller.sync_iroh_now().effects, ())
        self.assertEqual(harness.controller.copy_iroh_invite().effects, ())
        harness.controller.activate_replication_mode("offline", "iroh")
        with patch(
            "pomodorough.replication_controller.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            no_service_leave = harness.controller.leave_iroh_room()
        self.assertEqual(effect_types(no_service_leave), (LoadState, Render))
        harness.controller.iroh_status_changed("NOT CONNECTED")
        harness.controller.iroh_failure("background failure")
        self.assertEqual(harness.cloud.restores, 0)

        missing_join = harness.controller.join_iroh_room("invite")
        self.assertEqual(missing_join.effects[0].message, "network.iroh_not_packaged")

        iroh = FakeIroh()
        harness.iroh = iroh
        iroh.available = False
        self.assertFalse(harness.controller.iroh_transition_ready().value)
        self.assertEqual(harness.applied[-1].effects[0].message, "iroh unavailable")
        unavailable_join = harness.controller.join_iroh_room("invite")
        self.assertEqual(unavailable_join.effects[0].message, "iroh unavailable")

        iroh.available = True
        harness.controller.mode = "centralized"
        harness.cloud.busy = True
        busy_join = harness.controller.join_iroh_room("invite")
        self.assertEqual(busy_join.effects[0].message, "network.wait_cloud_join")

        harness.cloud.busy = False
        harness.controller.activate_replication_mode("offline", "centralized")
        self.assertEqual(harness.cloud.restores, 1)
        harness.store.failure = OSError("room create failed")
        failed_create = harness.controller.create_iroh_room("Room")
        self.assertEqual(failed_create.effects[0].message, "room create failed")

        harness.store.failure = ValueError("leave failed")
        with patch(
            "pomodorough.replication_controller.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            failed_leave = harness.controller.leave_iroh_room()
        self.assertEqual(failed_leave.effects[0].message, "leave failed")

        clipboard = SimpleClipboard()
        harness.controller.iroh_invite = "invite-to-copy"
        with patch(
            "pomodorough.replication_controller.QApplication",
            SimpleApplication(clipboard),
        ):
            copied = harness.controller.copy_iroh_invite()
        self.assertEqual(clipboard.text, "invite-to-copy")
        self.assertEqual(copied.effects[0], ShowStatus("network.invite_copied", 5000))

    def test_room_actions_and_signal_handlers_preserve_state_and_recovery(self) -> None:
        iroh = FakeIroh()
        harness = ControllerHarness(iroh=iroh)
        harness.controller.mode = "iroh"
        harness.store.active_iroh_room_id = "room-1"

        with patch(
            "pomodorough.replication_controller.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.assertEqual(harness.controller.leave_iroh_room().effects, ())
        self.assertNotIn(("leave",), harness.store.calls)

        with patch(
            "pomodorough.replication_controller.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            left = harness.controller.leave_iroh_room()
        self.assertEqual(effect_types(left), (LoadState, Render))
        self.assertEqual(harness.controller.mode, "offline")
        self.assertEqual(iroh.calls[-1], ("stop",))

        harness.controller.refresh_iroh_invite()
        self.assertEqual(iroh.calls[-1], ("refresh",))
        harness.controller.sync_iroh_now()
        self.assertEqual(harness.store.calls[-1], ("capture",))
        self.assertEqual(iroh.calls[-1], ("sync",))

        harness.store.failure = ValueError("capture failed")
        failed = harness.controller.sync_iroh_now()
        self.assertEqual(failed.effects[0].message, "capture failed")
        harness.store.failure = None

        harness.controller.iroh_details_changed({"peers": 2})
        self.assertEqual(harness.controller.iroh_details, {"peers": 2})
        harness.controller.iroh_details_changed([])  # type: ignore[arg-type]
        self.assertEqual(harness.controller.iroh_details, {})
        harness.controller.iroh_invite_ready("invite-text")
        self.assertEqual(harness.controller.iroh_invite, "invite-text")

        harness.controller.iroh_join_pending = True
        joined = harness.controller.iroh_joined()
        self.assertEqual(effect_types(joined), (LoadState, Render))
        self.assertEqual(harness.mode_selections[-1], "iroh")
        self.assertEqual(harness.invite_clears, 1)
        self.assertEqual(
            effect_types(harness.controller.iroh_projection_changed()),
            (LoadState, Render),
        )
        harness.controller.mode = "offline"
        self.assertEqual(harness.controller.iroh_projection_changed().effects, ())

        harness.controller.iroh_join_pending = True
        harness.store.replication_mode = "centralized"
        failure = harness.controller.iroh_failure("join failed")
        self.assertEqual(effect_types(failure), (RenderNetwork,))
        self.assertEqual(harness.applied[-1].effects[0], ShowStatus("join failed", 15_000))
        self.assertEqual(harness.cloud.restores, 1)


def effect_types(outcome: object) -> tuple[type[object], ...]:
    return tuple(type(effect) for effect in outcome.effects)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
