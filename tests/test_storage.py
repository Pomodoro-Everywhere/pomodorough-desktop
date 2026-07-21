from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pomodorough.core import rebuild_optimistic
from pomodorough.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_command_is_durable_before_reducer(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start", None, "focus", settings["durations"], now_ms=1_784_548_800_000
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [queued])
        timer, _history = rebuild_optimistic(None, [], loaded["pending"])
        self.assertEqual(timer["status"], "running")

    def test_sync_acknowledgement_removes_command(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start", None, "focus", settings["durations"], now_ms=1_784_548_800_000
        )
        self.store.apply_sync(
            {
                "acknowledgements": [
                    {"commandId": queued["id"], "outcome": "applied", "reason": ""}
                ],
                "revision": 1,
                "canonicalTimer": {
                    "id": queued["timerId"],
                    "phase": "focus",
                    "status": "running",
                    "plannedDurationMs": 1_500_000,
                    "elapsedAtAnchorMs": 0,
                    "anchorAt": queued["occurredAt"],
                },
                "history": [],
                "serverHlcWallMs": queued["hlcWallMs"],
            }
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["snapshot"]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
