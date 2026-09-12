"""D41: storage-mutation failures must capture infra, notice-only validation."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QMessageBox

from pomodorough.controller_outcomes import EmitNotice, LoadState, Synchronize
from pomodorough.iroh_protocol import IrohProtocolError, RoomInvite
from pomodorough.replication_controller import (
    ReplicationContext,
    ReplicationController,
    ReplicationPorts,
)
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)

INFRA_ERRORS = [OSError("infra boom"), sqlite3.Error("db boom")]


def _notice_messages(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


class _Strings:
    @staticmethod
    def text(key: str) -> str:
        return key


class _Cloud:
    def __init__(self) -> None:
        self.busy = False

    def restore(self) -> None:
        pass

    def stop_revision_stream(self) -> None:
        pass


class _Store:
    def __init__(self) -> None:
        self.active_iroh_room_id: str | None = None
        self.replication_mode = "offline"
        self.failure: Exception | None = None

    def set_replication_mode(self, mode: str) -> None:
        if self.failure is not None:
            raise self.failure

    def create_iroh_room(self, secret: bytes, name: str | None) -> str:
        if self.failure is not None:
            raise self.failure
        return "room-created"

    def prepare_iroh_join(self, *args: object) -> None:
        if self.failure is not None:
            raise self.failure

    def leave_iroh_room(self) -> None:
        if self.failure is not None:
            raise self.failure

    def capture_local_iroh_records(self) -> None:
        if self.failure is not None:
            raise self.failure


class _Iroh:
    def availability(self) -> tuple[bool, str]:
        return True, ""

    def start_room(self, room_id: str, *, emit_invite: bool = False) -> None:
        pass

    def stop(self) -> None:
        pass

    def join_room(self, invite: object) -> None:
        pass

    def sync_now(self) -> None:
        pass


class ReplicationHarness:
    def __init__(self) -> None:
        self.store = _Store()
        self.cloud = _Cloud()
        self.iroh: _Iroh | None = _Iroh()
        self.applied: list = []
        self.ports = ReplicationPorts(
            context=self.context,
            apply_outcome=self.applied.append,
            dialog_parent=lambda: None,  # type: ignore[arg-type]
            replication_mode_data=lambda index: "iroh",
            set_replication_mode=lambda mode: None,
            show_screen=lambda index, force: None,
            focus_create_room=lambda reason: None,
            room_name_text=lambda: "Room",
            invite_text=lambda: "invite",
            clear_invite_input=lambda: None,
            iroh_failure=lambda message: None,
        )
        self.controller = ReplicationController(self.ports)

    def context(self) -> ReplicationContext:
        return ReplicationContext(
            store=self.store,
            cloud=self.cloud,
            iroh=self.iroh,
            strings=_Strings(),
            closed=False,
        )


def _invite() -> RoomInvite:
    return RoomInvite(
        room_id="room-1",
        endpoint_ticket="ticket",
        endpoint_id="endpoint",
        room_secret=bytes(range(32)),
        room_name="Room",
    )


class ReplicationD41Tests(unittest.TestCase):
    def _check_infra(self, run) -> None:
        for error in INFRA_ERRORS:
            with (
                patch(
                    "pomodorough.replication_controller.capture_exception",
                ) as capture,
            ):
                outcome, applied = run(error)
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)
                texts = _notice_messages(outcome)
                applied_texts = [
                    e.message
                    for o in applied
                    for e in o.effects
                    if isinstance(e, EmitNotice)
                ]
                self.assertTrue(texts or applied_texts)

    def _check_validation(self, run) -> None:
        with patch(
            "pomodorough.replication_controller.capture_exception",
        ) as capture:
            outcome, applied = run(ValueError("validation boom"))
            capture.assert_not_called()
            texts = _notice_messages(outcome)
            applied_texts = [
                e.message for o in applied for e in o.effects
                if isinstance(e, EmitNotice)
            ]
            combined = texts + applied_texts
            self.assertTrue(any("validation boom" in m for m in combined))

    def test_mode_changed_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()
        harness.store.active_iroh_room_id = "room-1"

        def run(error: Exception):
            harness.store.failure = error
            harness.applied.clear()
            return harness.controller.replication_mode_changed("iroh"), []

        self._check_infra(run)
        self._check_validation(run)

    def test_saved_room_ready_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()

        def run(error: Exception):
            harness.applied.clear()
            with patch.object(
                ReplicationController,
                "_saved_iroh_room_ready_value",
                side_effect=error,
            ):
                value = harness.controller._iroh_transition_ready_value()
                self.assertFalse(value)
                return SimpleNamespace(effects=()), list(harness.applied)

        self._check_infra(run)
        self._check_validation(run)

    def test_create_room_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()

        def run(error: Exception):
            harness.store.failure = error
            return harness.controller.create_iroh_room("Room"), []

        self._check_infra(run)
        self._check_validation(run)

    def test_join_prepare_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()

        def run(error: Exception):
            harness.store.failure = error
            with patch(
                "pomodorough.replication_controller.parse_invite",
                return_value=_invite(),
            ):
                return harness.controller.join_iroh_room("inv"), []

        self._check_infra(run)
        self._check_validation(run)

    def test_join_invite_parse_stays_validation_only(self) -> None:
        harness = ReplicationHarness()
        with patch(
            "pomodorough.replication_controller.capture_exception",
        ) as capture:
            with patch(
                "pomodorough.replication_controller.parse_invite",
                side_effect=IrohProtocolError("bad invite"),
            ):
                outcome = harness.controller.join_iroh_room("bad")
        capture.assert_not_called()
        self.assertIn("bad invite", _notice_messages(outcome))

    def test_leave_cancel_join_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()
        harness.controller.iroh_join_pending = True

        def run(error: Exception):
            harness.store.failure = error
            harness.controller.iroh_join_pending = True
            return harness.controller.leave_iroh_room(), []

        self._check_infra(run)
        self._check_validation(run)

    def test_leave_room_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()

        def run(error: Exception):
            harness.store.failure = error
            with patch(
                "pomodorough.replication_controller.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                return harness.controller.leave_iroh_room(), []

        self._check_infra(run)
        self._check_validation(run)

    def test_sync_now_splits_infra_and_validation(self) -> None:
        harness = ReplicationHarness()

        def run(error: Exception):
            harness.store.failure = error
            return harness.controller.sync_iroh_now(), []

        self._check_infra(run)
        self._check_validation(run)


DURATIONS = {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}


def _timer(status: str) -> dict:
    return {
        "id": "timer-d41",
        "phase": "focus",
        "status": status,
        "plannedDurationMs": DURATIONS["focus"],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "start"},
    }


class TimerHarness:
    def __init__(self, status: str = "idle") -> None:
        self.store = Mock()
        self.store.has_pending_auto_break.return_value = False
        self.store.process_auto_break.return_value = []
        self.cloud = SimpleNamespace(authenticated=False, busy=False)
        self.current_timer: dict | None = _timer(status)
        self.replication_mode = "offline"
        self.known_tasks: dict = {}
        self.settings = {
            "selectedPhase": "focus",
            "durationsMs": dict(DURATIONS),
            "selectedTaskId": None,
        }
        self.ports = TimerInteractionPorts(
            context=self.context,
            apply_outcome=Mock(),
            mutation_blocked=lambda: False,
            issue_command=Mock(),
            maybe_auto_start_break=Mock(return_value=False),
            notice=Mock(),
            task_input_text=Mock(return_value="New task"),
            clear_task_input=Mock(),
            task_item_data=Mock(return_value=None),
            invalidate_task_selector=Mock(),
            render_task_selector=Mock(),
            refresh_duration_spins=Mock(),
            refresh_auto_breaks=Mock(),
            stop_sound_timer=Mock(),
            stop_completion_sound=Mock(),
            set_stop_sound_control=Mock(),
        )
        self.controller = TimerInteractionController(self.ports)

    def context(self) -> TimerInteractionContext:
        return TimerInteractionContext(
            store=self.store,
            cloud=self.cloud,
            closed=False,
            timer=self.current_timer,
            settings=self.settings,
            user=None,
            tasks=[],
            known_tasks=self.known_tasks,
            projection_now_ms=123_000,
            replication_mode=self.replication_mode,
            history_resolution_active=False,
        )


class TimerD41Tests(unittest.TestCase):
    def _assert_infra(self, outcome, error) -> None:
        self.assertTrue(
            any(isinstance(e, EmitNotice) for e in outcome.effects),
            "infra failure must surface a notice",
        )

    def test_expired_iroh_projection_splits_and_unmasks(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness("completed")
            harness.replication_mode = "iroh"
            harness.store.project_iroh_expiry.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.tick()
            capture.assert_called_once()
            self.assertIs(capture.call_args[0][0], error)
            self._assert_infra(outcome, error)
            self.assertFalse(
                any(isinstance(e, Synchronize) for e in outcome.effects),
                "failure must not claim Synchronize success",
            )
            self.assertFalse(harness.controller.auto_finish_in_progress)
        harness = TimerHarness("completed")
        harness.replication_mode = "iroh"
        harness.store.project_iroh_expiry.side_effect = ValueError("bad expiry")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.tick()
        capture.assert_not_called()
        self.assertIn("bad expiry", _notice_messages(outcome))
        self.assertFalse(
            any(isinstance(e, Synchronize) for e in outcome.effects),
        )

    def test_expired_iroh_success_still_synchronizes(self) -> None:
        harness = TimerHarness("completed")
        harness.replication_mode = "iroh"
        harness.store.project_iroh_expiry.side_effect = None
        outcome = harness.controller.tick()
        self.assertTrue(
            any(isinstance(e, Synchronize) for e in outcome.effects),
        )
        self.assertTrue(
            any(isinstance(e, LoadState) for e in outcome.effects),
        )

    def test_primary_action_restart_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness("cancelled")
            harness.store.queue_restart.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.primary_action()
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness("cancelled")
        harness.store.queue_restart.side_effect = ValueError("bad restart")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.primary_action()
        capture.assert_not_called()
        self.assertIn("bad restart", _notice_messages(outcome))

    def test_issue_queue_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness("idle")
            harness.store.queue_command.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.issue("start")
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness("idle")
        harness.store.queue_command.side_effect = ValueError("bad queue")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.issue("start")
        capture.assert_not_called()
        self.assertIn("bad queue", _notice_messages(outcome))

    def test_auto_break_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness()
            harness.store.process_auto_break.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.maybe_auto_start_break()
            capture.assert_called_once()
            self.assertFalse(outcome.value)
            self._assert_infra(outcome, error)
        harness = TimerHarness()
        harness.store.process_auto_break.side_effect = ValueError("bad break")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.maybe_auto_start_break()
        capture.assert_not_called()
        self.assertIn("bad break", _notice_messages(outcome))

    def test_task_selection_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness("running")
            harness.store.set_selected_task_id.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.task_selection_changed(0)
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness("running")
        harness.store.set_selected_task_id.side_effect = ValueError("bad select")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.task_selection_changed(0)
        capture.assert_not_called()
        self.assertIn("bad select", _notice_messages(outcome))

    def test_add_task_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness()
            harness.store.set_selected_task_id.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.task_from_title",
                return_value={"id": "task-1", "title": "Task"},
            ):
                with patch(
                    "pomodorough.timer_interaction_controller.capture_exception",
                ) as capture:
                    outcome = harness.controller.add_task("Task")
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness()
        harness.store.set_selected_task_id.side_effect = ValueError("bad add")
        with patch(
            "pomodorough.timer_interaction_controller.task_from_title",
            return_value={"id": "task-1", "title": "Task"},
        ):
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.add_task("Task")
        capture.assert_not_called()
        self.assertIn("bad add", _notice_messages(outcome))

    def test_delete_task_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness()
            harness.current_timer = _timer("idle")
            harness.known_tasks = {"task-1": {"id": "task-1", "title": "Task"}}
            harness.store.queue_task_operation.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.delete_task("task-1")
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness()
        harness.known_tasks = {"task-1": {"id": "task-1", "title": "Task"}}
        harness.store.queue_task_operation.side_effect = ValueError("bad delete")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.delete_task("task-1")
        capture.assert_not_called()
        self.assertIn("bad delete", _notice_messages(outcome))

    def test_duration_changed_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness()
            harness.store.queue_duration_operation.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.duration_changed("focus", 30)
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness()
        harness.store.queue_duration_operation.side_effect = ValueError("bad dur")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.duration_changed("focus", 30)
        capture.assert_not_called()
        self.assertIn("bad dur", _notice_messages(outcome))

    def test_auto_breaks_changed_splits(self) -> None:
        for error in INFRA_ERRORS:
            harness = TimerHarness()
            harness.store.set_auto_start_breaks.side_effect = error
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.auto_breaks_changed(True)
            capture.assert_called_once()
            self._assert_infra(outcome, error)
        harness = TimerHarness()
        harness.store.set_auto_start_breaks.side_effect = ValueError("bad auto")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.auto_breaks_changed(True)
        capture.assert_not_called()
        self.assertIn("bad auto", _notice_messages(outcome))


if __name__ == "__main__":
    unittest.main()
