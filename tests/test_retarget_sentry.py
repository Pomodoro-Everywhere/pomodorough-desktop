"""Immutable retarget must persist as an operation, never as an overlay."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough.controller_outcomes import EmitNotice, Render
from pomodorough.core import task_from_title
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)


def _notices(outcome) -> list[str]:
    return [
        effect.message
        for effect in outcome.effects
        if isinstance(effect, EmitNotice)
    ]

INFRA_ERRORS = [OSError("infra boom"), sqlite3.Error("db boom")]
VALIDATION_ERRORS = [ValueError("bad value"), KeyError("bad key")]


def _active_timer() -> dict:
    return {
        "id": "timer-retarget-1",
        "phase": "focus",
        "status": "running",
        "taskId": "task-original",
    }


class TerminalResolvedTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_resolution_uses_projection_task_without_overlay(self) -> None:
        resolve = LocalTimer._resolved_timer_task_id
        self.assertEqual(resolve(_active_timer()), "task-original")
        self.assertIsNone(resolve({**_active_timer(), "taskId": None}))
        self.assertIsNone(
            resolve({**_active_timer(), "phase": "short_break"})
        )
        self.assertIsNone(resolve({"phase": "focus"}))

    def test_metrics_use_projection_task(self) -> None:
        self.timer.known_tasks = {
            "task-original": {"id": "task-original", "title": "Orig"},
        }
        metrics = self.timer._timer_metrics(
            {**_active_timer(), "plannedDurationMs": 1_500_000},
            1_000,
        )
        self.assertEqual(metrics["taskId"], "task-original")
        self.assertEqual(metrics["taskTitle"], "Orig")


class TerminalRetargetOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_reload_reflects_immutable_retarget(self) -> None:
        first = task_from_title("Cache first")
        second = task_from_title("Cache second")
        self.store.queue_task_operation("upsert", first, now_ms=1)
        self.store.queue_task_operation("upsert", second, now_ms=2)
        self.store.set_selected_task_id(first["id"], now_ms=3)
        start = self.store.queue_command(
            "start",
            None,
            "focus",
            self.store.load()["settings"]["durationsMs"],
            first["id"],
            now_ms=4,
        )
        self.store.set_selected_task_id(second["id"], now_ms=5)
        self.timer.reload(now_ms=5)
        state = self.timer.state(now_ms=5)
        self.assertEqual(state["taskId"], second["id"])
        pending = self.store.load()["pending"]
        starts = [c for c in pending if c["type"] == "start"]
        self.assertEqual(starts[0].get("taskId"), first["id"])
        retargets = [c for c in pending if c["type"] == "retarget"]
        self.assertEqual(len(retargets), 1)
        self.assertEqual(retargets[0].get("timerId"), start["timerId"])
        self.assertEqual(retargets[0].get("taskId"), second["id"])


class StorageRetargetSentryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_set_selected_task_retarget_failure_rolls_back(self) -> None:
        task = task_from_title("Retarget keep task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"], now_ms=2)
        start_settings = self.store.load()["settings"]
        self.store.queue_command(
            "start",
            None,
            "focus",
            start_settings["durationsMs"],
            task["id"],
            now_ms=3,
        )
        before = self.store.load()
        for error in INFRA_ERRORS + VALIDATION_ERRORS:
            with (
                patch.object(
                    Store,
                    "_persist_retarget_command_locked",
                    side_effect=error,
                ),
                self.assertRaises(type(error)),
            ):
                self.store.set_selected_task_id(None, now_ms=1_000)
            self.assertEqual(self.store.load(), before)

    def test_projected_history_marks_pending_without_overlay(self) -> None:
        history = [{"timerId": "t-keep", "taskId": "orig"}]
        projection = SimpleNamespace(history=history)
        result = self.store.projected_history(projection, {"pending": []})
        self.assertEqual(result, history)
        result = self.store.projected_history(
            projection, {"pending": [{"timerId": "t-keep"}]}
        )
        self.assertEqual(
            result, [{"timerId": "t-keep", "taskId": "orig", "pending": True}]
        )

    def test_active_focus_fallback_infra_captures_none(self) -> None:
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    self.store, "load", side_effect=ValueError("bad core")
                ),
                patch.object(
                    self.store, "get_meta", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.assertIsNone(
                    self.store._active_focus_timer_locked(1_000)
                )
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)

    def test_active_focus_fallback_validation_silent(self) -> None:
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    self.store, "load", side_effect=ValueError("bad core")
                ),
                patch.object(
                    self.store, "get_meta", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.assertIsNone(
                    self.store._active_focus_timer_locked(1_000)
                )
                capture.assert_not_called()

    def test_active_focus_fallback_success_returns_snapshot(self) -> None:
        timer = {"id": "t-snap", "phase": "focus", "status": "running"}
        with (
            patch.object(
                self.store, "load", side_effect=ValueError("bad core")
            ),
            patch.object(
                self.store,
                "get_meta",
                return_value={"canonicalTimer": timer},
            ),
            patch(
                "pomodorough.storage.capture_exception",
            ) as capture,
        ):
            self.assertEqual(
                self.store._active_focus_timer_locked(1_000), timer
            )
            capture.assert_not_called()


def _harness() -> TimerInteractionController:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    ports = TimerInteractionPorts(
        context=lambda: TimerInteractionContext(
            store=store,
            cloud=SimpleNamespace(authenticated=False, busy=False),
            closed=False,
            timer=None,
            settings={
                "selectedPhase": "focus",
                "durationsMs": {"focus": 1_500_000},
                "selectedTaskId": None,
            },
            user=None,
            tasks=[],
            known_tasks={},
            projection_now_ms=0,
            replication_mode="offline",
            history_resolution_active=False,
        ),
        apply_outcome=Mock(),
        mutation_blocked=lambda: False,
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
    controller = TimerInteractionController(ports)
    controller._test_store = store  # type: ignore[attr-defined]
    return controller


class ControllerRetargetSentryTests(unittest.TestCase):
    def test_select_phase_splits_infra_and_validation(self) -> None:
        for error in INFRA_ERRORS:
            controller = _harness()
            controller._ports.context().store.set_selected_phase.side_effect = (
                error
            )
            with patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture:
                outcome = controller.select_phase("focus")
            capture.assert_called_once()
            self.assertIs(capture.call_args[0][0], error)
            self.assertIn(str(error), _notices(outcome))
            self.assertTrue(
                any(isinstance(item, Render) for item in outcome.effects)
            )
        controller = _harness()
        store = controller._ports.context().store
        store.set_selected_phase.side_effect = ValueError("bad phase")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = controller.select_phase("focus")
        capture.assert_not_called()
        self.assertIn("bad phase", _notices(outcome))
        self.assertTrue(
            any(isinstance(item, Render) for item in outcome.effects)
        )

    def test_queue_timer_command_splits_infra_and_validation(self) -> None:
        for error in INFRA_ERRORS:
            controller = _harness()
            with (
                patch.object(
                    TimerInteractionController,
                    "_queue_timer_command_value",
                    side_effect=error,
                ),
                patch(
                    "pomodorough.timer_interaction_controller.capture_exception",
                ) as capture,
            ):
                outcome = controller.queue_timer_command("start", False)
            capture.assert_called_once()
            self.assertIs(capture.call_args[0][0], error)
            self.assertFalse(outcome.value)
            self.assertIn(str(error), _notices(outcome))
        controller = _harness()
        with (
            patch.object(
                TimerInteractionController,
                "_queue_timer_command_value",
                side_effect=ValueError("bad queue"),
            ),
            patch(
                "pomodorough.timer_interaction_controller.capture_exception",
            ) as capture,
        ):
            outcome = controller.queue_timer_command("start", False)
        capture.assert_not_called()
        self.assertFalse(outcome.value)
        self.assertIn("bad queue", _notices(outcome))


if __name__ == "__main__":
    unittest.main()
