from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store, utc_timestamp


def _history_item(timer_id, command_id, phase, planned_ms, stamp):
    return {
        "id": timer_id,
        "timerId": timer_id,
        "phase": phase,
        "status": "completed",
        "plannedDurationMs": planned_ms,
        "commandId": command_id,
        "completedAt": stamp,
        "endedAt": stamp,
    }


def _focus_item(index, stamp):
    return _history_item(
        f"remote-focus-{index}",
        f"remote-finish-{index}",
        "focus",
        25 * 60_000,
        stamp,
    )


class RemoteFinishPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path, shared_core=SharedCore())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _apply_remote(self, history, revision, timer=None):
        request = self.store.sync_payload()
        response = {
            "acknowledgements": [
                {"commandId": c["id"], "outcome": "applied", "reason": ""}
                for c in request["commands"]
            ],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": revision,
            "canonicalTimer": timer,
            "history": history,
            "tasks": [],
            "durationsMs": {
                "focus": 25 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(5_000 + revision * 1_000),
            "serverHlcWallMs": 5_000 + revision * 1_000,
            "serverHlcCounter": 0,
        }
        self.store.apply_sync(response, request)

    def test_remote_single_focus_advances_to_short_break(self):
        history = [_focus_item(1, "1970-01-01T00:00:02.000Z")]
        self._apply_remote(history, 1)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )

    def test_remote_fourth_focus_advances_to_long_break(self):
        history = [
            _focus_item(1, "1970-01-01T00:01:00.000Z"),
            _focus_item(2, "1970-01-01T00:02:00.000Z"),
            _focus_item(3, "1970-01-01T00:03:00.000Z"),
            _focus_item(4, "1970-01-01T00:04:00.000Z"),
        ]
        self._apply_remote(history, 1)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "long_break"
        )

    def test_remote_empty_history_is_noop(self):
        self._apply_remote([], 1)
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")

    def test_remote_non_focus_latest_advances_to_focus(self):
        self.store.set_selected_phase("short_break")
        history = [
            _history_item(
                "remote-break-1",
                "remote-break-finish-1",
                "short_break",
                5 * 60_000,
                "1970-01-01T00:00:03.000Z",
            )
        ]
        self._apply_remote(history, 1)
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")

    def test_remote_finish_restart_uses_derived_phase(self):
        history = [_focus_item(1, "1970-01-01T00:00:02.000Z")]
        self._apply_remote(history, 1)
        settings = self.store.load()["settings"]
        self.assertEqual(settings["selectedPhase"], "short_break")
        retained = {
            "id": "remote-focus-1",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 25 * 60_000,
            "elapsedAtAnchorMs": 25 * 60_000,
            "anchorAt": "1970-01-01T00:00:02.000Z",
        }
        _cleared, started = self.store.queue_restart(
            retained,
            settings["selectedPhase"],
            settings["durationsMs"],
            now_ms=6_000,
        )
        self.assertEqual(started["phase"], "short_break")
        self.store.close()
        reopened = Store(self.path, shared_core=SharedCore())
        try:
            self.assertEqual(
                reopened.load()["settings"]["selectedPhase"], "short_break"
            )
            pending = reopened.load()["pending"]
            starts = [c for c in pending if c["type"] == "start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["phase"], "short_break")
        finally:
            reopened.close()
            self.store = Store(self.path, shared_core=SharedCore())

    def test_remote_finish_idempotent_and_preserves_explicit(self):
        history = [_focus_item(1, "1970-01-01T00:00:02.000Z")]
        self._apply_remote(history, 1)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )
        self._apply_remote(history, 2)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )
        self.store.set_selected_phase("long_break")
        self._apply_remote(history, 3)
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "long_break"
        )


if __name__ == "__main__":
    unittest.main()
