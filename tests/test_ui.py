from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from pomodorough.core import task_from_title
from pomodorough.storage import Store
from pomodorough.ui import MainWindow


class FakeCloud(QObject):
    signed_in = Signal(object)
    signed_out = Signal()
    sync_ready = Signal(object)
    revision_available = Signal(object)
    authorization_stale = Signal()
    failure = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.authenticated = True
        self.busy = False
        self.payloads: list[dict[str, object]] = []

    def restore(self) -> None:
        pass

    def sync(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)

    def login(self) -> None:
        pass

    def logout(self) -> None:
        pass


class MainWindowDurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.cloud = FakeCloud()
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.cloud, QIcon())

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def test_duration_spin_queues_and_triggers_sync(self) -> None:
        self.window.duration_spins["focus"].setValue(30)

        operation = self.store.load()["pendingDurations"][0]
        self.assertEqual(operation["durationMs"], 30 * 60_000)
        self.assertEqual(self.cloud.payloads[-1]["durationOperations"], [operation])
        self.assertFalse(self.window._account_synced)

    def test_unauthenticated_sync_does_not_queue_unowned_request(self) -> None:
        self.cloud.authenticated = False
        self.cloud.busy = True

        self.window._sync()

        self.assertEqual(self.cloud.payloads, [])
        self.assertIsNone(self.window._sync_request)

        self.cloud.authenticated = True
        self.cloud.busy = False
        self.window._sync()
        self.assertEqual(len(self.cloud.payloads), 1)
        self.assertIsNotNone(self.window._sync_request)

    def test_newer_remote_revision_triggers_sync(self) -> None:
        before = len(self.cloud.payloads)

        self.cloud.revision_available.emit(self.window.revision)
        self.assertEqual(len(self.cloud.payloads), before)

        self.cloud.revision_available.emit(self.window.revision + 1)
        self.assertEqual(len(self.cloud.payloads), before + 1)

    def test_periodic_sync_pulls_without_local_changes(self) -> None:
        self.window.sync_timer.timeout.emit()

        self.assertEqual(
            self.cloud.payloads[-1],
            {
                "deviceId": self.store.device_id,
                "lastRevision": 0,
                "commands": [],
                "taskOperations": [],
                "durationOperations": [],
            },
        )

    def test_opening_tasks_pulls_without_local_changes(self) -> None:
        before = len(self.cloud.payloads)

        self.window._show_screen(1)

        self.assertEqual(len(self.cloud.payloads), before + 1)

    def test_synced_task_can_be_selected_while_timer_is_paused(self) -> None:
        task = task_from_title("Remote task")
        self.window._sync()
        self.window._apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": {
                    "id": "timer-remote",
                    "phase": "focus",
                    "status": "paused",
                    "plannedDurationMs": 25 * 60_000,
                    "elapsedAtAnchorMs": 60_000,
                    "anchorAt": "2026-07-22T06:26:34.649Z",
                    "taskId": None,
                },
                "history": [],
                "tasks": [task],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "serverHlcWallMs": 1_000,
                "serverHlcCounter": 0,
            }
        )

        task_index = self.window.task_combo.findData(task["id"])
        self.assertGreater(task_index, 0)
        self.assertTrue(self.window.task_combo.isEnabled())

        self.window.task_combo.setCurrentIndex(task_index)

        self.assertEqual(self.window.settings["selectedTaskId"], task["id"])
        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], task["id"]
        )

        self.window._render_task_selector(
            {
                "phase": "focus",
                "status": "running",
                "taskId": task["id"],
            },
            True,
        )
        self.assertFalse(self.window.task_combo.isEnabled())

    def test_stale_stream_authorization_triggers_sync(self) -> None:
        before = len(self.cloud.payloads)

        self.cloud.authorization_stale.emit()

        self.assertEqual(len(self.cloud.payloads), before + 1)

    def test_remote_durations_refresh_spins_without_queueing(self) -> None:
        self.window._sync()
        before = len(self.cloud.payloads)
        self.window._apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": {
                    "focus": 30 * 60_000,
                    "short_break": 10 * 60_000,
                    "long_break": 20 * 60_000,
                },
                "serverHlcWallMs": 1_000,
                "serverHlcCounter": 0,
            }
        )

        self.assertEqual(
            {phase: spin.value() for phase, spin in self.window.duration_spins.items()},
            {"focus": 30, "short_break": 10, "long_break": 20},
        )
        self.assertEqual(self.store.load()["pendingDurations"], [])
        self.assertEqual(len(self.cloud.payloads), before)
        self.assertTrue(self.window._account_synced)

    def test_in_flight_edit_is_replayed_and_sent_next(self) -> None:
        self.window.duration_spins["focus"].setValue(26)
        sent = self.cloud.payloads[-1]["durationOperations"][0]
        replacement = self.store.queue_duration_operation("focus", 27 * 60_000)

        self.window._apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [
                    {
                        "operationId": sent["id"],
                        "outcome": "applied",
                        "reason": "",
                    }
                ],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": {
                    "focus": 30 * 60_000,
                    "short_break": 6 * 60_000,
                    "long_break": 16 * 60_000,
                },
                "serverHlcWallMs": 1_000,
                "serverHlcCounter": 0,
            }
        )

        self.assertEqual(self.store.load()["pendingDurations"], [replacement])
        self.assertEqual(self.window.duration_spins["focus"].value(), 27)
        self.assertEqual(
            self.cloud.payloads[-1]["durationOperations"], [replacement]
        )
        self.assertFalse(self.window._account_synced)

    def test_cancelled_timer_resets_clock(self) -> None:
        timer = None
        start = self.store.queue_command(
            "start", timer, "focus", {"focus": 60_000}, now_ms=1_000
        )
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "lastIntent": None,
            "taskId": None,
        }
        self.store.queue_command(
            "pause", timer, "focus", {"focus": 60_000}, now_ms=31_000
        )
        self.window._load_state()

        with patch("pomodorough.storage.time.time", return_value=31):
            self.window._issue("cancel")

        self.assertEqual(self.window.timer["status"], "cancelled")
        self.assertEqual(self.window.clock.time_text, "01:00")
        self.assertEqual(self.window.clock.progress, 0)


if __name__ == "__main__":
    unittest.main()
