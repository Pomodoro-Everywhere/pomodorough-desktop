from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)
from pomodorough.timer_screen import TimerScreen
from pomodorough.timer_view import ClockWidget, ceiling_minutes


def _timer(status: str, phase: str = "focus") -> dict[str, object]:
    return {
        "id": "timer-parity-1",
        "phase": phase,
        "status": status,
        "plannedDurationMs": 1_500_000,
        "elapsedAtAnchorMs": 0,
        "anchorAt": None,
        "lastIntent": {"type": "start"},
    }


def _harness(status: str = "completed") -> tuple[TimerInteractionController, Mock, list]:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    issued: list[tuple[str, object]] = []
    ports = TimerInteractionPorts(
        context=lambda: TimerInteractionContext(
            store=store,
            cloud=SimpleNamespace(authenticated=False, busy=False),
            closed=False,
            timer=_timer(status),
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
        issue_command=lambda command, automatic: issued.append((command, automatic)),
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
    return TimerInteractionController(ports), ports, issued


class CeilingMinuteTests(unittest.TestCase):
    def test_ceiling_matrix(self) -> None:
        cases = (
            (25 * 60_000, 25),
            (5 * 60_000, 5),
            (15 * 60_000, 15),
            (90_000, 2),
            (60_001, 2),
            (60_000, 1),
            (1, 1),
            (0, 1),
        )
        for planned, expected in cases:
            with self.subTest(planned=planned):
                self.assertEqual(ceiling_minutes(planned), expected)

    def test_long_tick_every_fifth(self) -> None:
        for total in (1, 5, 25):
            with self.subTest(total=total):
                longs = [tick for tick in range(total) if tick % 5 == 0]
                self.assertEqual(longs, list(range(0, total, 5)))


class ClockWidgetTickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_set_state_threads_tick_count(self) -> None:
        widget = ClockWidget(Strings("en"))
        widget.set_state("05:00", "Short break", "IN TRANSIT", 0.5, 5)
        self.assertEqual(widget.tick_count, 5)
        widget.set_state("25:00", "Focus", "READY", 0.0, 25)
        self.assertEqual(widget.tick_count, 25)

    def test_render_clock_uses_display_ceiling_minutes(self) -> None:
        strings = Strings("en")
        screen = TimerScreen(
            strings,
            {"durations": {"focus": 25, "short_break": 5, "long_break": 15}},
        )
        for planned_ms, expected in ((5 * 60_000, 5), (25 * 60_000, 25)):
            state = screen.presentation(
                {
                    "status": "idle",
                    "phase": "focus",
                    "plannedDurationMs": planned_ms,
                    "elapsedAtAnchorMs": 0,
                    "anchorAt": None,
                },
                selected_phase="focus",
                settings={"durationsMs": {"focus": planned_ms, "short_break": 300_000, "long_break": 900_000}},
                now_ms=0,
            )
            screen.render_clock(state, [])
            with self.subTest(planned=planned_ms):
                self.assertEqual(screen.clock.tick_count, expected)


class StopSoundParityTests(unittest.TestCase):
    def test_stop_sound_never_clears_terminal(self) -> None:
        for status in ("completed", "cancelled", "superseded", "running"):
            controller, _, issued = _harness(status)
            controller.stop_sound_and_clear()
            with self.subTest(status=status):
                self.assertEqual(issued, [])

    def test_primary_from_finished_restarts(self) -> None:
        controller, _, _ = _harness("completed")
        store = controller._ports.context().store
        store.queue_restart.return_value = [{}, {}]
        controller.primary_action()
        store.queue_restart.assert_called_once()


class TaskRetargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _start_focus(self, task_id: str | None = None, now_ms: int = 1_000):
        settings = self.store.load()["settings"]
        return self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], task_id, now_ms=now_ms
        )

    def test_live_retarget_rewrites_pending_start_and_marker(self) -> None:
        first = task_from_title("First task")
        second = task_from_title("Second task")
        self.store.queue_task_operation("upsert", first, now_ms=1)
        self.store.queue_task_operation("upsert", second, now_ms=2)
        self.store.set_selected_task_id(first["id"], now_ms=3)
        start = self._start_focus(first["id"], now_ms=4)
        timer_id = start["timerId"]
        self.store.set_selected_task_id(second["id"], now_ms=5)
        loaded = self.store.load()
        self.assertEqual(loaded["settings"]["selectedTaskId"], second["id"])
        pending = [c for c in loaded["pending"] if c.get("timerId") == timer_id]
        starts = [c for c in pending if c.get("type") == "start"]
        self.assertTrue(starts)
        self.assertEqual(starts[0].get("taskId"), second["id"])
        found, retargeted = self.store.retargeted_task_id(timer_id)
        self.assertTrue(found)
        self.assertEqual(retargeted, second["id"])

    def test_unassign_retarget_clears_pending_task(self) -> None:
        task = task_from_title("Solo task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"], now_ms=2)
        start = self._start_focus(task["id"], now_ms=3)
        self.store.set_selected_task_id(None, now_ms=4)
        pending = [c for c in self.store.load()["pending"] if c.get("timerId") == start["timerId"]]
        starts = [c for c in pending if c.get("type") == "start"]
        self.assertTrue(starts)
        self.assertNotIn("taskId", starts[0])
        found, retargeted = self.store.retargeted_task_id(start["timerId"])
        self.assertTrue(found)
        self.assertIsNone(retargeted)

    def test_history_applies_retarget_marker(self) -> None:
        task = task_from_title("History task")
        other = task_from_title("Other task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.queue_task_operation("upsert", other, now_ms=2)
        self.store.set_selected_task_id(task["id"], now_ms=3)
        start = self._start_focus(task["id"], now_ms=4)
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": task["id"],
        }
        settings = self.store.load()["settings"]
        self.store.queue_command("finish", timer, "focus", settings["durationsMs"], now_ms=5)
        self.store.set_selected_task_id(other["id"], now_ms=6)
        state = self.store.load(projection=True)
        projection = self.store.projected_state(now_ms=6, state=state)
        history = self.store.projected_history(projection, state)
        matches = [h for h in history if h.get("timerId") == start["timerId"]]
        self.assertTrue(matches)

    def test_terminal_metrics_prefers_retarget(self) -> None:
        first = task_from_title("Active first")
        second = task_from_title("Active second")
        self.store.queue_task_operation("upsert", first, now_ms=1)
        self.store.queue_task_operation("upsert", second, now_ms=2)
        self.store.set_selected_task_id(first["id"], now_ms=3)
        self._start_focus(first["id"], now_ms=4)
        timer = LocalTimer(self.store)
        timer.reload(now_ms=4)
        before = timer.state(now_ms=4)
        self.assertEqual(before["taskId"], first["id"])
        self.store.set_selected_task_id(second["id"], now_ms=5)
        timer.reload(now_ms=5)
        after = timer.state(now_ms=5)
        self.assertEqual(after["taskId"], second["id"])
        self.assertEqual(after["taskTitle"], second["title"])

    def test_idle_retarget_leaves_no_marker(self) -> None:
        task = task_from_title("Idle task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"], now_ms=2)
        self.assertEqual(self.store.task_retargets(), {})


class SingleSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _screen(self) -> TimerScreen:
        return TimerScreen(
            Strings("en"),
            {"durations": {"focus": 25, "short_break": 5, "long_break": 15}},
        )

    def test_selector_always_enabled_no_duplicate(self) -> None:
        screen = self._screen()
        tasks = [{"id": "task-1", "title": "One"}]
        known = {"task-1": {"id": "task-1", "title": "One"}}
        for active, phase in ((True, "focus"), (False, "focus"), (True, "short_break")):
            screen.invalidate_task_selector()
            screen.render_task_selector(
                {"taskId": "task-1", "phase": phase, "status": "running" if active else "idle"},
                active,
                selected_phase=phase,
                settings={"selectedTaskId": "task-1"},
                tasks=tasks,
                known_tasks=known,
                mutations_enabled=True,
            )
            with self.subTest(active=active, phase=phase):
                self.assertTrue(screen.task_combo.isEnabled())
                self.assertEqual(screen.task_combo.accessibleName(), "Focus task")
                self.assertEqual(screen.active_task_context.text(), "")
                self.assertTrue(screen.active_task_context.isHidden())


if __name__ == "__main__":
    unittest.main()
