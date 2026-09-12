"""Retarget best-effort fallbacks must capture infra, stay silent on validation."""

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


class TerminalLoadRetargetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_infra_captures_and_falls_back_empty(self) -> None:
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.terminal.capture_exception",
                ) as capture,
            ):
                self.assertEqual(self.timer._load_retargets(), {})
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)

    def test_validation_stays_silent(self) -> None:
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.terminal.capture_exception",
                ) as capture,
            ):
                self.assertEqual(self.timer._load_retargets(), {})
                capture.assert_not_called()


class TerminalResolvedTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)
        self.timer._cached_retargets = None

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_infra_captures_and_keeps_timer_task(self) -> None:
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    self.store, "retargeted_task_id", side_effect=error
                ),
                patch(
                    "pomodorough.terminal.capture_exception",
                ) as capture,
            ):
                result = self.timer._resolved_timer_task_id(
                    _active_timer(), retargets=False
                )
                self.assertEqual(result, "task-original")
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)

    def test_validation_stays_silent(self) -> None:
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    self.store, "retargeted_task_id", side_effect=error
                ),
                patch(
                    "pomodorough.terminal.capture_exception",
                ) as capture,
            ):
                result = self.timer._resolved_timer_task_id(
                    _active_timer(), retargets=False
                )
                self.assertEqual(result, "task-original")
                capture.assert_not_called()


class TerminalRetargetCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_cached_metrics_avoid_per_frame_db(self) -> None:
        cached = {"timer-retarget-1": "task-retargeted"}
        self.timer._cached_retargets = cached
        self.timer.known_tasks = {
            "task-original": {"id": "task-original", "title": "Orig"},
            "task-retargeted": {"id": "task-retargeted", "title": "New"},
        }
        with (
            patch.object(
                self.store,
                "retargeted_task_id",
                side_effect=AssertionError("per-frame DB hit"),
            ),
            patch.object(
                self.store,
                "task_retargets",
                side_effect=AssertionError("per-frame DB hit"),
            ),
        ):
            resolved = self.timer._resolved_timer_task_id(_active_timer())
            self.assertEqual(resolved, "task-retargeted")
            metrics = self.timer._timer_metrics(
                {**_active_timer(), "plannedDurationMs": 1_500_000},
                1_000,
            )
            self.assertEqual(metrics["taskId"], "task-retargeted")

    def test_retarget_from_map_edges(self) -> None:
        resolve = LocalTimer._retarget_from_map
        self.assertEqual(resolve("t", "orig", {}), "orig")
        self.assertEqual(resolve("t", "orig", {"t": "new"}), "new")
        self.assertIsNone(resolve("t", "orig", {"t": None}))
        self.assertIsNone(resolve("t", "orig", {"t": ""}))
        self.assertIsNone(resolve("t", "orig", {"t": 123}))
        self.assertIsNone(resolve("t", None, {"t": None}))

    def test_reload_caches_markers(self) -> None:
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
        self.assertEqual(
            self.timer._cached_retargets.get(start["timerId"]), second["id"]
        )


class StorageRetargetSentryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_set_selected_task_infra_captures_keeps_write(self) -> None:
        task = task_from_title("Retarget keep task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    Store,
                    "_retarget_active_focus_task_locked",
                    side_effect=error,
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.store.set_selected_task_id(task["id"], now_ms=1_000)
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)
                settings = self.store.load()["settings"]
                self.assertEqual(settings["selectedTaskId"], task["id"])

    def test_set_selected_task_validation_silent_keeps_write(self) -> None:
        task = task_from_title("Retarget keep task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    Store,
                    "_retarget_active_focus_task_locked",
                    side_effect=error,
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.store.set_selected_task_id(task["id"], now_ms=2_000)
                capture.assert_not_called()
                settings = self.store.load()["settings"]
                self.assertEqual(settings["selectedTaskId"], task["id"])

    def test_projected_history_infra_captures_falls_back(self) -> None:
        projection = SimpleNamespace(history=[])
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.assertEqual(
                    self.store.projected_history(
                        projection, {"pending": []}
                    ),
                    [],
                )
                capture.assert_called_once()
                self.assertIs(capture.call_args[0][0], error)

    def test_projected_history_validation_silent(self) -> None:
        projection = SimpleNamespace(history=[])
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                self.assertEqual(
                    self.store.projected_history(
                        projection, {"pending": []}
                    ),
                    [],
                )
                capture.assert_not_called()

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

    def test_projected_history_fallback_preserves_items(self) -> None:
        history = [{"timerId": "t-keep", "taskId": "orig"}]
        projection = SimpleNamespace(history=history)
        for error in INFRA_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                result = self.store.projected_history(
                    projection, {"pending": []}
                )
                self.assertEqual(result, history)
                capture.assert_called_once()
        for error in VALIDATION_ERRORS:
            with (
                patch.object(
                    self.store, "task_retargets", side_effect=error
                ),
                patch(
                    "pomodorough.storage.capture_exception",
                ) as capture,
            ):
                result = self.store.projected_history(
                    projection, {"pending": []}
                )
                self.assertEqual(result, history)
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
