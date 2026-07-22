from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from pomodorough.cli import main
from pomodorough.storage import Store


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = main(arguments, store=self.store, stdout=stdout, stderr=stderr)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_start_and_json_status(self) -> None:
        result, output, error = self.invoke("start", "long-break", "--minutes", "2")
        self.assertEqual(result, 0)
        self.assertIn("Long break | running", output)
        self.assertEqual(error, "")

        result, output, _error = self.invoke("status", "--json")
        state = json.loads(output)
        self.assertEqual(result, 0)
        self.assertEqual(state["phase"], "long_break")
        self.assertEqual(state["plannedDurationMs"], 120_000)
        stored = self.store.load()
        self.assertEqual(stored["settings"]["durations"]["long_break"], 15)
        self.assertEqual(
            stored["settings"]["durationsMs"]["long_break"], 15 * 60_000
        )
        self.assertEqual(stored["pendingDurations"], [])

    def test_invalid_transition_returns_error(self) -> None:
        result, output, error = self.invoke("finish")
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("Cannot finish timer while it is idle", error)

    def test_pending_resolution_status_is_safe_and_mutation_is_action_error(
        self,
    ) -> None:
        self.store.prepare_resolution({"id": "user-1"}, 4, "merge")

        result, output, error = self.invoke("status")

        self.assertEqual(result, 0)
        self.assertIn("Account history resolution pending", output)
        self.assertEqual(error, "")

        result, output, error = self.invoke("start")
        self.assertEqual(result, 2)
        self.assertEqual(output, "")
        self.assertIn("Resolve pending account history", error)


if __name__ == "__main__":
    unittest.main()
