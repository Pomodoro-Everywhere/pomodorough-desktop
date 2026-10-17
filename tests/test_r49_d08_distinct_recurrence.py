"""R49-D08 residual: time recurrence, clock/room isolation, KeyError budget."""

from __future__ import annotations

import sqlite3
import unittest

from pomodorough.sentry_monitoring import (
    note_clock_recovery,
    note_room_read_recovery,
    reset_clock_incident_budget,
    reset_room_read_incident_budget,
    should_capture_clock_failure,
    should_capture_room_read_failure,
)


class RoomReadRecurrenceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_room_read_incident_budget()
        reset_clock_incident_budget()

    def tearDown(self) -> None:
        reset_room_read_incident_budget()
        reset_clock_incident_budget()

    def test_same_signature_suppressed_then_recurs_after_interval(self) -> None:
        self.assertTrue(
            should_capture_room_read_failure(
                sqlite3.OperationalError("denied"), now_monotonic=100.0
            )
        )
        self.assertFalse(
            should_capture_room_read_failure(
                sqlite3.OperationalError("denied again"), now_monotonic=101.0
            )
        )
        self.assertTrue(
            should_capture_room_read_failure(
                sqlite3.OperationalError("denied later"), now_monotonic=105.0
            )
        )

    def test_key_error_preserves_budget_for_next_infra(self) -> None:
        self.assertFalse(should_capture_room_read_failure(KeyError("bad")))
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("boom"))
        )
        self.assertFalse(
            should_capture_room_read_failure(
                sqlite3.OperationalError("boom again")
            )
        )

    def test_room_and_clock_budgets_are_isolated(self) -> None:
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("room down"))
        )
        self.assertTrue(
            should_capture_clock_failure(OSError("clock down"))
        )
        self.assertFalse(
            should_capture_room_read_failure(sqlite3.OperationalError("room again"))
        )
        self.assertFalse(
            should_capture_clock_failure(OSError("clock again"))
        )
        note_room_read_recovery()
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("room back"))
        )
        self.assertFalse(
            should_capture_clock_failure(OSError("clock still"))
        )
        note_clock_recovery()
        self.assertTrue(
            should_capture_clock_failure(OSError("clock back"))
        )


if __name__ == "__main__":
    unittest.main()
