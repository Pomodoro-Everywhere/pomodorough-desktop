from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough.controller_outcomes import EmitNotice, LoadState, Render, Synchronize
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)

DURATIONS = {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}


def timer(status: str, *, phase: str = "focus") -> dict[str, object]:
    return {
        "id": "timer-matrix-0001",
        "phase": phase,
        "status": status,
        "plannedDurationMs": DURATIONS[phase],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "start"},
    }


class TimerHarness:
    def __init__(self, status: str = "idle") -> None:
        self.store = Mock()
        self.store.has_pending_auto_break.return_value = False
        self.store.process_auto_break.return_value = []
        self.store.owns_timer.return_value = False
        self.cloud = SimpleNamespace(authenticated=False, busy=False)
        self.closed = False
        self.current_timer: dict[str, object] | None = timer(status)
        self.settings = {"selectedPhase": "focus", "durationsMs": dict(DURATIONS), "selectedTaskId": None}
        self.user = None
        self.tasks: list[dict[str, str]] = []
        self.known_tasks: dict[str, dict[str, str]] = {}
        self.replication_mode = "offline"
        self.history_resolution_active = False
        self.blocked = False
        self.applied: list[object] = []
        self.issued: list[tuple[str, bool | None]] = []
        self.invalidations = 0
        self.rendered_selectors: list[tuple[dict[str, object], bool]] = []
        self.ports = TimerInteractionPorts(
            context=self.context,
            apply_outcome=self.applied.append,
            mutation_blocked=lambda: self.blocked,
            issue_command=lambda command, automatic: self.issued.append((command, automatic)),
            maybe_auto_start_break=Mock(return_value=False),
            notice=Mock(),
            task_input_text=Mock(return_value="New task"),
            clear_task_input=Mock(),
            task_item_data=Mock(return_value=None),
            invalidate_task_selector=self.invalidate,
            render_task_selector=lambda value, active: self.rendered_selectors.append((value, active)),
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
            closed=self.closed,
            timer=self.current_timer,
            settings=self.settings,
            user=self.user,
            tasks=self.tasks,
            known_tasks=self.known_tasks,
            projection_now_ms=123_000,
            replication_mode=self.replication_mode,
            history_resolution_active=self.history_resolution_active,
        )

    def invalidate(self) -> None:
        self.invalidations += 1


class TimerInteractionBranchMatrixTests(unittest.TestCase):
    def test_tick_short_circuits_closed_resolution_and_duplicate_completion(self) -> None:
        harness = TimerHarness("running")
        harness.closed = True
        self.assertEqual(harness.controller.tick().effects, ())
        harness.closed = False
        harness.history_resolution_active = True
        self.assertEqual(tuple(map(type, harness.controller.tick().effects)), (Render,))
        harness.history_resolution_active = False
        harness.current_timer = timer("completed")
        harness.controller.auto_finish_in_progress = True
        self.assertEqual(harness.controller.tick().effects, ())

    def test_expired_iroh_timer_projects_and_recovers_projection_failure(self) -> None:
        harness = TimerHarness("completed")
        harness.replication_mode = "iroh"
        outcome = harness.controller.tick()
        self.assertEqual(
            tuple(map(type, outcome.effects)), (LoadState, Render, Synchronize)
        )
        harness.store.project_iroh_expiry.assert_called_once_with(123_000)
        harness.store.project_iroh_expiry.side_effect = ValueError("projection failed")
        harness.controller.tick()
        harness.ports.notice.assert_called_with("projection failed")
        self.assertFalse(harness.controller.auto_finish_in_progress)

    def test_primary_action_routes_each_status_and_terminal_boundaries(self) -> None:
        harness = TimerHarness()
        for status, expected in (("running", "pause"), ("paused", "resume"), ("idle", "start")):
            harness.current_timer = timer(status)
            harness.controller.primary_action()
            self.assertEqual(harness.issued[-1], (expected, None))
        harness.current_timer = timer("cancelled")
        harness.blocked = True
        self.assertEqual(harness.controller.primary_action().effects, ())
        harness.blocked = False
        harness.store.queue_restart.side_effect = OSError("restart failed")
        outcome = harness.controller.primary_action()
        self.assertIsInstance(outcome.effects[0], EmitNotice)
        harness.current_timer = {"status": "mystery"}
        self.assertEqual(harness.controller.primary_action().effects, ())

    def test_issue_rejects_invalid_handles_queue_failure_and_automatic_noop(self) -> None:
        harness = TimerHarness("idle")
        harness.controller.auto_finish_in_progress = True
        harness.blocked = True
        self.assertEqual(harness.controller.issue("start").effects, ())
        self.assertFalse(harness.controller.auto_finish_in_progress)
        harness.blocked = False
        self.assertEqual(harness.controller.issue("pause").effects, ())
        harness.store.queue_command.side_effect = ValueError("queue failed")
        self.assertIsInstance(harness.controller.issue("start").effects[0], EmitNotice)
        harness.store.queue_command.side_effect = None
        harness.store.queue_command.return_value = None
        self.assertEqual(
            tuple(map(type, harness.controller.issue("start", True).effects)),
            (LoadState, Render),
        )

    def test_task_selection_covers_blocked_stale_and_persistence_failure(self) -> None:
        harness = TimerHarness("running")
        harness.blocked = True
        harness.controller.task_selection_changed(0)
        self.assertEqual(harness.invalidations, 1)
        self.assertTrue(harness.rendered_selectors[-1][1])
        harness.blocked = False
        harness.ports.task_item_data.return_value = "missing-task"
        harness.controller.task_selection_changed(0)
        harness.store.set_selected_task_id.assert_called_with(None)
        harness.store.set_selected_task_id.side_effect = OSError("selection failed")
        outcome = harness.controller.task_selection_changed(0)
        self.assertIsInstance(outcome.effects[-1], EmitNotice)
        self.assertEqual(harness.invalidations, 2)

    def test_add_task_covers_blocked_duplicate_new_and_failure_paths(self) -> None:
        harness = TimerHarness()
        harness.blocked = True
        self.assertEqual(harness.controller.add_task().effects, ())
        harness.blocked = False
        with patch("pomodorough.timer_interaction_controller.task_from_title", return_value={"id": "task-1", "title": "Task"}):
            harness.tasks = [{"id": "task-1", "title": "Task"}]
            harness.controller.add_task("Task")
            harness.store.queue_task_operation.assert_not_called()
            harness.tasks = []
            harness.controller.add_task("Task")
            harness.store.queue_task_operation.assert_called_once()
            harness.store.set_selected_task_id.side_effect = ValueError("task failed")
            self.assertIsInstance(harness.controller.add_task("Task").effects[0], EmitNotice)

    def test_delete_task_covers_blocked_missing_selected_and_unselected(self) -> None:
        harness = TimerHarness()
        harness.blocked = True
        self.assertEqual(harness.controller.delete_task("task-1").effects, ())
        harness.blocked = False
        self.assertEqual(harness.controller.delete_task("missing").effects, ())
        harness.known_tasks["task-1"] = {"id": "task-1", "title": "Task"}
        harness.settings["selectedTaskId"] = "task-1"
        harness.controller.delete_task("task-1")
        harness.store.set_selected_task_id.assert_called_once_with(None)
        harness.settings["selectedTaskId"] = "task-2"
        harness.controller.delete_task("task-1")
        self.assertEqual(harness.store.set_selected_task_id.call_count, 1)
        harness.store.queue_task_operation.side_effect = OSError("delete failed")
        self.assertIsInstance(harness.controller.delete_task("task-1").effects[0], EmitNotice)

    def test_duration_auto_break_and_sound_terminal_branches(self) -> None:
        harness = TimerHarness()
        harness.current_timer = None
        effects = harness.controller.duration_changed("focus", 30).effects
        self.assertEqual(tuple(map(type, effects)), (Render, Synchronize))
        harness.store.process_auto_break.return_value = [{"id": "start"}]
        self.assertNotIn(Synchronize, tuple(map(type, harness.controller.maybe_auto_start_break(sync=False).effects)))
        harness.current_timer = timer("completed")
        harness.controller.stop_sound_and_clear()
        self.assertEqual(harness.issued[-1], ("clear", None))
        harness.current_timer = timer("running")
        before = len(harness.issued)
        harness.controller.stop_sound_and_clear()
        self.assertEqual(len(harness.issued), before)


if __name__ == "__main__":
    unittest.main()
