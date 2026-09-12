"""D50: tick only reads has_pending_auto_break on unowned idle path."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough.controller_outcomes import EmitNotice, Render
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)

DURATIONS = {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}


def _running_timer() -> dict[str, object]:
    return {
        "id": "timer-d50-0001",
        "phase": "focus",
        "status": "running",
        "plannedDurationMs": DURATIONS["focus"],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "start"},
    }


def _harness(
    *,
    user: dict[str, object] | None,
    authenticated: bool,
    busy: bool,
) -> SimpleNamespace:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    cloud = SimpleNamespace(authenticated=authenticated, busy=busy)
    state = SimpleNamespace(timer=_running_timer())
    settings = {
        "selectedPhase": "focus",
        "durationsMs": dict(DURATIONS),
        "selectedTaskId": None,
    }

    def context() -> TimerInteractionContext:
        return TimerInteractionContext(
            store, cloud, False, state.timer, settings, user,
            [], {}, 0, "offline", False,
        )

    ports = TimerInteractionPorts(
        context=context,
        apply_outcome=Mock(),
        mutation_blocked=Mock(return_value=False),
        issue_command=Mock(),
        maybe_auto_start_break=Mock(return_value=False),
        notice=Mock(),
        task_input_text=Mock(return_value=""),
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
    return SimpleNamespace(
        store=store, controller=TimerInteractionController(ports),
    )


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


class D50TickPendingGuardTests(unittest.TestCase):
    def test_authenticated_tick_does_zero_pending_reads(self) -> None:
        harness = _harness(
            user={"id": "user-1"}, authenticated=True, busy=False,
        )
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("db")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.tick()
        harness.store.has_pending_auto_break.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(_notices(outcome), [])
        self.assertIn(Render, tuple(map(type, outcome.effects)))

    def test_busy_tick_does_zero_pending_reads(self) -> None:
        harness = _harness(user=None, authenticated=False, busy=True)
        harness.store.has_pending_auto_break.side_effect = sqlite3.Error("db")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.tick()
        harness.store.has_pending_auto_break.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(_notices(outcome), [])

    def test_unowned_idle_tick_still_reads_pending(self) -> None:
        harness = _harness(user=None, authenticated=False, busy=False)
        outcome = harness.controller.tick()
        harness.store.has_pending_auto_break.assert_called_once()
        self.assertEqual(_notices(outcome), [])


if __name__ == "__main__":
    unittest.main()
