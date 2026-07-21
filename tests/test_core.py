from __future__ import annotations

import unittest

from pomodorough.core import elapsed_ms, format_remaining, next_break_phase, reduce_command


def command(kind: str, sequence: int = 1, observed: int = 0) -> dict:
    return {
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


if __name__ == "__main__":
    unittest.main()
