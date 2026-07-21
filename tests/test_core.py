from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pomodorough.core import (
    elapsed_ms,
    format_remaining,
    next_break_phase,
    normalize_task_title,
    rebuild_tasks,
    reduce_command,
    task_from_title,
    task_summaries_today,
)


def command(
    kind: str, sequence: int = 1, observed: int = 0, task_id: str | None = None
) -> dict:
    result = {
        "id": f"command-{sequence:08d}",
        "deviceSequence": sequence,
        "timerId": "timer-00000001",
        "type": kind,
        "phase": "focus",
        "plannedDurationMs": 1_500_000,
        "occurredAt": "2026-07-20T12:00:00.000Z",
        "hlcWallMs": 1_000,
        "hlcCounter": 0,
        "observedElapsedMs": observed,
    }
    if task_id:
        result["taskId"] = task_id
    return result


class CoreTests(unittest.TestCase):
    def test_timer_command_lifecycle(self) -> None:
        timer, history = reduce_command(None, [], command("start"))
        self.assertEqual(timer["status"], "running")
        timer, history = reduce_command(timer, history, command("pause", 2, 123_000))
        self.assertEqual(timer["elapsedAtAnchorMs"], 123_000)
        self.assertEqual(timer["status"], "paused")
        timer, history = reduce_command(timer, history, command("resume", 3, 123_000))
        self.assertEqual(timer["status"], "running")
        timer, history = reduce_command(timer, history, command("finish", 4, 200_000))
        self.assertEqual(timer["status"], "completed")
        self.assertEqual(len(history), 1)

    def test_elapsed_uses_positive_wall_time_and_clamps(self) -> None:
        timer = {
            "status": "running",
            "plannedDurationMs": 60_000,
            "elapsedAtAnchorMs": 10_000,
            "anchorAt": "1970-01-01T00:00:10.000Z",
        }
        self.assertEqual(elapsed_ms(timer, 5_000), 10_000)
        self.assertEqual(elapsed_ms(timer, 100_000), 60_000)

    def test_fourth_focus_uses_long_break(self) -> None:
        history = [{"phase": "focus", "status": "completed"} for _ in range(4)]
        self.assertEqual(next_break_phase(history), "long_break")
        self.assertEqual(next_break_phase(history[:3]), "short_break")

    def test_remaining_rounds_up(self) -> None:
        self.assertEqual(format_remaining(60_001), "01:01")
        self.assertEqual(format_remaining(0), "00:00")

    def test_task_identity_normalizes_unicode_and_printability(self) -> None:
        task = task_from_title("\x00Cafe\u0301\x1f")
        self.assertEqual(task["title"], "Café")
        self.assertEqual(task["id"], "aaf83054-24b2-8c0e-901f-a974147bfe82")
        self.assertEqual(normalize_task_title("\u00a0 spaced \u00a0"), " spaced ")

    def test_task_projection_uses_hlc_order_and_allows_recreation(self) -> None:
        task = task_from_title("Write release notes")
        operations = [
            {
                "id": "operation-3",
                "taskId": task["id"],
                "type": "upsert",
                "title": task["title"],
                "hlcWallMs": 20,
                "hlcCounter": 1,
            },
            {
                "id": "operation-1",
                "taskId": task["id"],
                "type": "upsert",
                "title": task["title"],
                "hlcWallMs": 10,
                "hlcCounter": 0,
            },
            {
                "id": "operation-2",
                "taskId": task["id"],
                "type": "delete",
                "hlcWallMs": 20,
                "hlcCounter": 0,
            },
        ]
        self.assertEqual(rebuild_tasks([], operations), [task])

    def test_timer_task_reaches_history_and_daily_summary(self) -> None:
        task = task_from_title("Release")
        timer, history = reduce_command(None, [], command("start", task_id=task["id"]))
        timer, history = reduce_command(
            timer, history, command("finish", 2, task_id="ignored-task-id")
        )
        self.assertEqual(timer["taskId"], task["id"])
        self.assertEqual(history[0]["taskId"], task["id"])
        summaries = task_summaries_today(
            [task], history, datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(summaries[task["id"]], {"finished": 1, "timeMs": 1_500_000})


if __name__ == "__main__":
    unittest.main()
