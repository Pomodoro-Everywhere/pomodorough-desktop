from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from pomodorough.core import rebuild_optimistic, task_from_title
from pomodorough.storage import Store, utc_timestamp
from pomodorough.terminal import LocalTimer
from pomodorough.ui import MainWindow


def wire_preference_operation(operation: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in operation.items() if key != "deviceId"}


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class FakeCloud(QObject):
    signed_in = Signal(object)
    signed_out = Signal()
    session_expired = Signal()
    sync_ready = Signal(object)
    bootstrap_ready = Signal(object)
    bootstrap_resolved = Signal(object)
    bootstrap_conflict = Signal(object)
    revision_available = Signal(object)
    authorization_stale = Signal()
    failure = Signal(str)

    def __init__(self, authenticated: bool = True, busy: bool = False) -> None:
        super().__init__()
        self.authenticated = authenticated
        self.busy = busy
        self.payloads: list[dict[str, object]] = []
        self.bootstrap_previews = 0
        self.resolutions: list[dict[str, object]] = []
        self.login_calls = 0
        self.logout_calls = 0
        self.restore_calls = 0
        self.revision_stops = 0
        self.revision_starts = 0

    def restore(self) -> None:
        self.restore_calls += 1

    def sync(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)

    def preview_bootstrap(self) -> None:
        self.bootstrap_previews += 1

    def resolve_bootstrap(self, payload: dict[str, object]) -> None:
        self.resolutions.append(payload)

    def login(self) -> None:
        self.login_calls += 1

    def logout(self) -> None:
        self.logout_calls += 1

    def stop_revision_stream(self) -> None:
        self.revision_stops += 1

    def start_revision_stream(self) -> None:
        self.revision_starts += 1


class FakeIroh(QObject):
    status_changed = Signal(str)
    details_changed = Signal(object)
    invite_ready = Signal(str)
    joined = Signal()
    projection_changed = Signal()
    failure = Signal(str)

    def __init__(self, available: bool = True) -> None:
        super().__init__()
        self.available = available
        self.started: list[tuple[str, bool]] = []
        self.joined_invites = []
        self.stop_calls = 0
        self.sync_calls = 0

    def availability(self) -> tuple[bool, str]:
        return self.available, "Iroh test transport ready" if self.available else "Iroh test transport unavailable"

    def start_room(self, room_id: str, *, emit_invite: bool = False) -> None:
        self.started.append((room_id, emit_invite))

    def join_room(self, invite) -> None:
        self.joined_invites.append(invite)

    def stop(self) -> None:
        self.stop_calls += 1

    def sync_now(self) -> None:
        self.sync_calls += 1

    def refresh_invite(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class MainWindowDurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(
            Path(self.temporary.name) / "state.sqlite3",
            iroh_secret_store=MemorySecretStore(),
        )
        self.cloud = FakeCloud()
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.cloud, QIcon())

    def tearDown(self) -> None:
        self.window.quitting = True
        self.window.close()
        self.store.close()
        self.temporary.cleanup()

    def _replace_window(self, cloud: FakeCloud) -> None:
        self.window.quitting = True
        self.window.close()
        self.cloud = cloud
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.cloud, QIcon())

    @staticmethod
    def _history_item(
        item_id: str, completed_at: str = "2026-07-22T10:00:00.000Z"
    ) -> dict[str, object]:
        return {
            "id": item_id,
            "timerId": f"timer-{item_id}",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 25 * 60_000,
            "completedAt": completed_at,
        }

    @staticmethod
    def _bootstrap_response(
        *, revision: int = 1, history: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        now_ms = int(time.time() * 1000)
        return {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": revision,
            "canonicalTimer": None,
            "history": history or [],
            "tasks": [],
            "durationsMs": {
                "focus": 25 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(now_ms),
            "serverHlcWallMs": now_ms,
            "serverHlcCounter": 0,
        }

    def _queue_completed_timer(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": None,
        }
        self.store.queue_command(
            "finish", timer, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.window._load_state()
        self.window._render()

    def test_completed_focus_clock_displays_next_break_duration(self) -> None:
        self._queue_completed_timer()

        self.assertEqual(self.window.clock.phase_text, "SHORT BREAK")
        self.assertEqual(self.window.clock.time_text, "05:00")

    def test_tray_retains_owned_context_menu(self) -> None:
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            self.window._build_tray()

        self.assertIsNotNone(self.window.tray)
        self.assertIsNotNone(self.window.tray_menu)
        self.assertIs(self.window.tray_menu.parent(), self.window)
        self.assertIs(self.window.tray.contextMenu(), self.window.tray_menu)

    def test_first_unowned_sign_in_previews_before_any_sync(self) -> None:
        self.store.queue_task_operation("upsert", task_from_title("Local task"))

        self.window._signed_in({"id": "user-1"})

        self.assertEqual(self.cloud.bootstrap_previews, 1)
        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.cloud.resolutions, [])
        self.assertTrue(self.window._history_resolution_active)

    def test_persisted_resolution_blocks_immediately_without_token(self) -> None:
        task = task_from_title("Persisted pending task")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        owner = {"id": "user-1", "email": "one@example.com"}
        request = self.store.prepare_resolution(owner, 4, "merge")
        before = self.store.load()
        self._replace_window(FakeCloud(authenticated=False))

        self.assertTrue(self.window._history_resolution_active)
        self.assertEqual(self.window._resolution_user, owner)
        self.assertFalse(self.window.primary_button.isEnabled())
        self.assertFalse(self.window.auto_breaks.isEnabled())
        self.window.task_input.setText("Blocked task")
        with patch.object(QMessageBox, "warning"):
            self.window._issue("start")
            self.window._add_task()
            self.window._duration_changed("focus", 30)
            self.window._select_phase("long_break")
            self.window._auto_breaks_changed(True)
        self.window._sync()
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.cloud.payloads, [])

        self.window._session_expired()
        self.assertTrue(self.window._history_resolution_active)
        self.assertEqual(
            self.store.pending_resolution(owner["id"])["request"], request
        )
        self.assertEqual(self.store.load()["pendingTasks"], [operation])
        self.window._account_action()
        self.assertEqual(self.cloud.login_calls, 1)

    def test_persisted_resolution_blocks_during_delayed_profile_verification(
        self,
    ) -> None:
        task = task_from_title("Delayed verification task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        owner = {"id": "user-1"}
        request = self.store.prepare_resolution(owner, 4, "merge")
        self._replace_window(FakeCloud(authenticated=False, busy=True))
        before = self.store.load()

        self.assertTrue(self.window._history_resolution_active)
        with patch.object(QMessageBox, "warning"):
            self.window._issue("start")
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.cloud.resolutions, [])

        self.cloud.busy = False
        self.cloud.authenticated = True
        self.window._signed_in(owner)
        self.assertEqual(self.cloud.resolutions, [request])

    def test_resolution_persisted_after_launch_activates_gate_before_sync(self) -> None:
        task = task_from_title("Concurrent claim")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.prepare_resolution({"id": "user-1"}, 4, "merge")
        before = self.store.load()

        self.window._sync()

        self.assertTrue(self.window._history_resolution_active)
        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.cloud.resolutions, [request])
        with patch.object(QMessageBox, "warning"):
            self.window._issue("start")
        self.assertEqual(self.store.load(), before)

    def test_malformed_persisted_resolution_starts_in_blocking_state(self) -> None:
        task = task_from_title("Corrupted resolution task")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_meta("pendingResolution", [])
        self._replace_window(FakeCloud(authenticated=True))
        before = self.store.load()

        self.assertTrue(self.window._history_resolution_active)
        self.assertTrue(self.window._resolution_retry_paused)
        self.assertIn("corrupted", self.window._resolution_corruption)
        with patch.object(QMessageBox, "warning"):
            self.window._issue("start")
            self.window._add_task()
        self.window._sync()

        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.store.load()["pendingTasks"], [operation])
        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.cloud.bootstrap_previews, 0)
        self.assertEqual(self.cloud.resolutions, [])

    def test_signed_out_local_mode_is_usable_without_pending_resolution(self) -> None:
        self._replace_window(FakeCloud(authenticated=False))

        self.assertFalse(self.window._history_resolution_active)
        self.window._issue("start")

        self.assertEqual(len(self.store.load()["pending"]), 1)
        self.assertEqual(self.cloud.payloads, [])

    def test_session_expiry_preserves_exact_request_for_same_user_reauth(self) -> None:
        task = task_from_title("Session expiry task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        owner = {"id": "user-1"}
        request = self.store.prepare_resolution(owner, 4, "merge")
        self._replace_window(FakeCloud(authenticated=True))
        self.window._signed_in(owner)
        self.assertEqual(self.cloud.resolutions, [request])

        self.cloud.authenticated = False
        self.window._session_expired()
        self.assertTrue(self.window._history_resolution_active)
        self.assertEqual(
            self.store.pending_resolution(owner["id"])["request"], request
        )
        self.window._account_action()
        self.assertEqual(self.cloud.login_calls, 1)

        self.cloud.authenticated = True
        self.window._signed_in(owner)
        self.assertEqual(self.cloud.resolutions, [request, request])

    def test_session_expiry_preserves_complete_owner_bound_state(self) -> None:
        owner = {"id": "user-1", "email": "one@example.com"}
        task = task_from_title("Preserved task")
        self.store.set_user(owner)
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.queue_duration_operation("focus", 30 * 60_000, now_ms=2)
        self._queue_completed_timer()
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            task["id"],
            now_ms=3_000,
        )
        self.window._load_state()
        before = self.store.load()
        before_timer = self.window.timer
        before_history = self.window.history
        before_tasks = self.window.tasks

        self.cloud.authenticated = False
        self.window._session_expired()

        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.window.user, owner)
        self.assertEqual(self.window.timer, before_timer)
        self.assertEqual(self.window.history, before_history)
        self.assertEqual(self.window.tasks, before_tasks)
        self.assertEqual(self.window.settings["durationsMs"]["focus"], 30 * 60_000)

    def test_different_user_quarantines_old_resolution_until_explicit_switch(
        self,
    ) -> None:
        task = task_from_title("Different-user task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        old_owner = {"id": "user-old"}
        old_request = self.store.prepare_resolution(old_owner, 4, "merge")
        before = self.store.load()
        self._replace_window(FakeCloud(authenticated=False))

        self.cloud.authenticated = True
        new_owner = {"id": "user-new"}
        self.window._signed_in(new_owner)

        self.assertEqual(self.store.load(), before)
        self.assertEqual(
            self.store.pending_resolution(old_owner["id"])["request"], old_request
        )
        self.assertEqual(self.cloud.resolutions, [])
        self.assertEqual(self.cloud.bootstrap_previews, 0)
        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.window._account_switch_user, new_owner)

        with patch.object(
            self.window, "_choose_account_switch_action", return_value="switch"
        ):
            self.window._account_action()

        self.assertIsNone(self.store.pending_resolution())
        self.assertEqual(self.store.load()["pendingTasks"], [])
        self.assertEqual(self.cloud.bootstrap_previews, 1)
        self.assertEqual(self.window._resolution_user, new_owner)

        self.window._bootstrap_ready(self._bootstrap_response(revision=9))

        fresh = self.store.pending_resolution(new_owner["id"])
        self.assertEqual(fresh["owner"], new_owner)
        self.assertNotEqual(fresh["request"]["requestId"], old_request["requestId"])
        self.assertEqual(fresh["request"]["expectedRevision"], 9)
        self.assertEqual(fresh["request"]["taskOperations"], [])
        self.assertEqual(self.cloud.resolutions, [fresh["request"]])

    def test_signing_out_mismatched_account_preserves_quarantined_data(self) -> None:
        task = task_from_title("Quarantined task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        owner = {"id": "user-old"}
        request = self.store.prepare_resolution(owner, 4, "merge")
        before = self.store.load()
        self._replace_window(FakeCloud(authenticated=True))
        self.window._signed_in({"id": "user-new"})

        with patch.object(
            self.window, "_choose_account_switch_action", return_value="sign_out"
        ):
            self.window._account_action()

        self.assertEqual(self.cloud.logout_calls, 1)
        self.cloud.authenticated = False
        self.window._signed_out()
        self.assertEqual(self.store.load(), before)
        self.assertEqual(
            self.store.pending_resolution(owner["id"])["request"], request
        )
        self.assertTrue(self.window._history_resolution_active)

    def test_same_owner_syncs_normally_and_explicit_account_switch_resets(self) -> None:
        old_user = {"id": "user-old"}
        self.store.set_user(old_user)
        self.store.queue_task_operation("upsert", task_from_title("Old account"))
        self.window._load_state()

        self.window._signed_in(old_user)
        self.assertEqual(self.cloud.bootstrap_previews, 0)
        self.assertEqual(len(self.cloud.payloads), 1)
        self.assertEqual(len(self.cloud.payloads[-1]["taskOperations"]), 1)

        self.cloud.payloads.clear()
        self.window._signed_in({"id": "user-new"})
        self.assertEqual(self.cloud.payloads, [])
        with patch.object(
            self.window, "_choose_account_switch_action", return_value="switch"
        ):
            self.window._account_action()
        self.assertEqual(self.cloud.bootstrap_previews, 1)
        loaded = self.store.load()
        self.assertIsNone(loaded["snapshot"]["user"])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [])

    def test_local_only_and_remote_only_resolve_without_prompt(self) -> None:
        user = {"id": "user-1"}
        self._queue_completed_timer()
        self.window._signed_in(user)
        with patch.object(self.window, "_prompt_history_resolution") as prompt:
            self.window._bootstrap_ready(self._bootstrap_response(revision=4))
        prompt.assert_not_called()
        self.assertEqual(self.cloud.resolutions[-1]["strategy"], "replace_remote")
        self.assertEqual(len(self.cloud.resolutions[-1]["commands"]), 2)

        self.store.clear_pending_resolution()
        self.store.reset_account_data()
        self.cloud.resolutions.clear()
        self.window._load_state()
        self.window._history_resolution_active = False
        self.window._signed_in(user)
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )
        with patch.object(self.window, "_prompt_history_resolution") as prompt:
            self.window._bootstrap_ready(remote)
        prompt.assert_not_called()
        self.assertEqual(self.cloud.resolutions[-1]["strategy"], "keep_remote")
        self.assertEqual(self.cloud.resolutions[-1]["commands"], [])

    def test_one_sided_history_with_opposing_task_requires_prompt(self) -> None:
        user = {"id": "user-1"}
        self.store.queue_task_operation(
            "upsert", task_from_title("Local task"), now_ms=100
        )
        self.window._signed_in(user)
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )
        with patch.object(
            self.window, "_prompt_history_resolution", return_value=None
        ) as prompt:
            self.window._bootstrap_ready(remote)

        prompt.assert_called_once_with()
        self.assertEqual(self.cloud.resolutions, [])
        self.assertTrue(self.window._resolution_retry_paused)

    def test_resolution_success_binds_owner_and_installs_canonical_state(self) -> None:
        user = {"id": "user-1", "email": "one@example.com"}
        self._queue_completed_timer()
        self.window._signed_in(user)
        self.window._bootstrap_ready(self._bootstrap_response(revision=4))
        request = self.cloud.resolutions[-1]
        response = self._bootstrap_response(
            revision=5, history=[self._history_item("canonical")]
        )
        response["acknowledgements"] = [
            {"commandId": item["id"], "outcome": "applied", "reason": ""}
            for item in request["commands"]
        ]

        self.window._apply_resolution(response)

        loaded = self.store.load()
        self.assertFalse(self.window._history_resolution_active)
        self.assertEqual(loaded["snapshot"]["user"], user)
        self.assertEqual(loaded["snapshot"]["revision"], 5)
        self.assertEqual(loaded["snapshot"]["history"][0]["id"], "canonical")
        self.assertIsNone(loaded["pendingResolution"])
        self.assertTrue(self.window._account_synced)

    def test_both_histories_block_sync_and_cancel_has_no_side_effects(self) -> None:
        self._queue_completed_timer()
        before = self.store.load()
        self.window._signed_in({"id": "user-1"})
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )

        with patch.object(
            self.window, "_prompt_history_resolution", return_value=None
        ):
            self.window._bootstrap_ready(remote)

        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.cloud.resolutions, [])
        self.assertEqual(self.store.load(), before)
        self.assertFalse(self.window.primary_button.isEnabled())
        self.assertFalse(self.window.add_task_button.isEnabled())
        self.assertTrue(
            all(not button.isEnabled() for button in self.window.phase_buttons.values())
        )
        self.assertFalse(self.window.auto_breaks.isEnabled())
        self.assertTrue(self.window._resolution_retry_paused)
        self.window.task_input.setText("Blocked task")
        with patch.object(QMessageBox, "warning"):
            self.window._issue("start")
            self.window._add_task()
            self.window._duration_changed("focus", 30)
            self.window._select_phase("long_break")
            self.window._auto_breaks_changed(True)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(
            self.window._selected_phase(), before["settings"]["selectedPhase"]
        )
        self.assertFalse(self.window.auto_breaks.isChecked())

    def test_cancelled_chooser_can_resume_without_signing_out(self) -> None:
        self._queue_completed_timer()
        self.window._signed_in({"id": "user-1"})
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )
        with patch.object(
            self.window, "_prompt_history_resolution", return_value=None
        ):
            self.window._bootstrap_ready(remote)
        self.assertTrue(self.window._resolution_retry_paused)
        self.assertIsNone(self.store.pending_resolution())

        with (
            patch.object(
                self.window,
                "_choose_resolution_account_action",
                return_value="continue",
            ),
            patch.object(
                self.window, "_prompt_history_resolution", return_value="merge"
            ) as chooser,
            patch.object(
                self.window, "_confirm_history_resolution", return_value=True
            ),
        ):
            self.window._account_action()

        chooser.assert_called_once_with()
        self.assertFalse(self.window._resolution_retry_paused)
        self.assertEqual(self.cloud.resolutions[-1]["strategy"], "merge")
        self.assertIsNotNone(self.store.pending_resolution("user-1"))

    def test_pending_resolution_account_action_can_sign_out(self) -> None:
        self.store.queue_task_operation(
            "upsert", task_from_title("Discarded on sign-out"), now_ms=1
        )
        owner = {"id": "user-1"}
        self.store.prepare_resolution(owner, 4, "merge")
        self.window._signed_in(owner)

        with patch.object(
            self.window,
            "_choose_resolution_account_action",
            return_value="sign_out",
        ):
            self.window._account_action()

        self.assertEqual(self.cloud.logout_calls, 1)
        self.cloud.authenticated = False
        self.window._signed_out()
        loaded = self.store.load()
        self.assertIsNone(loaded["pendingResolution"])
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertIsNone(loaded["snapshot"]["user"])

    def test_both_histories_require_confirmation_before_persisting(self) -> None:
        self._queue_completed_timer()
        self.window._signed_in({"id": "user-1"})
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )

        with (
            patch.object(
                self.window,
                "_prompt_history_resolution",
                return_value="replace_remote",
            ),
            patch.object(
                self.window, "_confirm_history_resolution", return_value=False
            ) as confirm,
        ):
            self.window._bootstrap_ready(remote)

        confirm.assert_called_once_with("replace_remote")
        self.assertIsNone(self.store.pending_resolution())
        self.assertEqual(self.cloud.resolutions, [])

    def test_resolution_confirmations_include_destructive_and_merge_warnings(self) -> None:
        for strategy, expected in (
            ("replace_remote", "will be replaced by this device's data"),
            ("keep_remote", "will be replaced by account data"),
            ("merge", "Conflicts or rejected changes are possible"),
        ):
            with self.subTest(strategy=strategy):
                with patch.object(
                    QMessageBox,
                    "warning",
                    return_value=QMessageBox.StandardButton.Yes,
                ) as warning:
                    self.assertTrue(
                        self.window._confirm_history_resolution(strategy)
                    )
                self.assertIn(expected, warning.call_args.args[2])
                self.assertEqual(
                    warning.call_args.args[4], QMessageBox.StandardButton.Cancel
                )

    def test_conflict_discards_stale_request_and_repreviews_without_data_loss(
        self,
    ) -> None:
        self._queue_completed_timer()
        self.window._signed_in({"id": "user-1"})
        remote = self._bootstrap_response(
            revision=5, history=[self._history_item("remote")]
        )
        with (
            patch.object(
                self.window, "_prompt_history_resolution", return_value="merge"
            ),
            patch.object(
                self.window, "_confirm_history_resolution", return_value=True
            ),
        ):
            self.window._bootstrap_ready(remote)
        stale = self.store.pending_resolution()
        local = self.store.load()["pending"]
        preview_count = self.cloud.bootstrap_previews

        self.window._bootstrap_conflict(
            {"status": 409, "message": "revision conflict"}
        )

        self.assertIsNone(self.store.pending_resolution())
        self.assertEqual(self.store.load()["pending"], local)
        self.assertEqual(self.cloud.payloads, [])
        self.assertEqual(self.cloud.bootstrap_previews, preview_count + 1)
        self.assertEqual(self.window._resolution_phase, "preview")

        fresh_remote = self._bootstrap_response(
            revision=6, history=[self._history_item("fresh-remote")]
        )
        with (
            patch.object(
                self.window, "_prompt_history_resolution", return_value="merge"
            ),
            patch.object(
                self.window, "_confirm_history_resolution", return_value=True
            ),
        ):
            self.window._bootstrap_ready(fresh_remote)

        fresh = self.store.pending_resolution("user-1")
        self.assertNotEqual(
            fresh["request"]["requestId"], stale["request"]["requestId"]
        )
        self.assertEqual(fresh["request"]["expectedRevision"], 6)
        self.assertEqual(fresh["request"]["commands"], local)

    def test_network_failure_preserves_exact_pending_resolution(self) -> None:
        self._queue_completed_timer()
        self.window._signed_in({"id": "user-1"})
        self.window._bootstrap_ready(self._bootstrap_response(revision=5))
        pending = self.store.pending_resolution("user-1")
        local = self.store.load()["pending"]

        self.window._cloud_failure("network unavailable")

        self.assertEqual(self.store.pending_resolution("user-1"), pending)
        self.assertEqual(self.store.load()["pending"], local)
        self.assertEqual(self.window._resolution_phase, "resolve")

    def test_duration_spin_queues_and_triggers_sync(self) -> None:
        self.window.duration_spins["focus"].setValue(30)

        operation = self.store.load()["pendingDurations"][0]
        self.assertEqual(operation["durationMs"], 30 * 60_000)
        self.assertEqual(self.cloud.payloads[-1]["durationOperations"], [operation])
        self.assertFalse(self.window._account_synced)

    def test_auto_start_toggle_syncs_and_remote_preference_refreshes_checkbox(
        self,
    ) -> None:
        self.window.auto_breaks.setChecked(True)

        operation = self.store.load()["pendingAutoStarts"][0]
        self.assertEqual(
            self.cloud.payloads[-1]["autoStartOperations"],
            [wire_preference_operation(operation)],
        )
        response = self._bootstrap_response(revision=1)
        response["autoStartAcknowledgements"] = [
            {"operationId": operation["id"], "outcome": "applied", "reason": ""}
        ]
        response["autoStartBreaks"] = True
        self.window._apply_sync(response)
        self.assertTrue(self.window.auto_breaks.isChecked())
        self.assertEqual(self.store.load()["pendingAutoStarts"], [])

        self.window._sync()
        remote = self._bootstrap_response(revision=2)
        remote["autoStartBreaks"] = False
        self.window._apply_sync(remote)
        self.assertFalse(self.window.auto_breaks.isChecked())
        self.assertFalse(self.window.settings["autoStartBreaks"])

    def test_auto_start_operation_survives_lost_malformed_and_expired_sync(
        self,
    ) -> None:
        owner = {"id": "user-1"}
        self.store.set_user(owner)
        self.window._load_state()
        self.window.auto_breaks.setChecked(True)
        operation = self.store.load()["pendingAutoStarts"][0]
        sent = self.cloud.payloads[-1]

        self.window._cloud_failure("network unavailable")
        self.assertEqual(self.store.load()["pendingAutoStarts"], [operation])
        self.window._sync()
        self.assertEqual(
            self.cloud.payloads[-1]["autoStartOperations"],
            [wire_preference_operation(operation)],
        )
        self.assertEqual(self.cloud.payloads[-1]["autoStartOperations"], sent["autoStartOperations"])

        malformed = self._bootstrap_response(revision=1)
        malformed.pop("autoStartBreaks")
        with patch.object(QMessageBox, "warning"):
            self.window._apply_sync(malformed)
        self.assertEqual(self.store.load()["pendingAutoStarts"], [operation])

        self.cloud.authenticated = False
        self.window._session_expired()
        self.assertEqual(self.store.load()["pendingAutoStarts"], [operation])
        self.assertEqual(self.window.user, owner)

    def test_synced_local_focus_completion_auto_starts_one_short_break(self) -> None:
        owner = {"id": "user-1"}
        self.store.set_user(owner)
        self.window._load_state()
        self.window.auto_breaks.setChecked(True)
        preference = self.store.load()["pendingAutoStarts"][0]
        preference_response = self._bootstrap_response(revision=1)
        preference_response["autoStartAcknowledgements"] = [
            {
                "operationId": preference["id"],
                "outcome": "applied",
                "reason": "",
            }
        ]
        preference_response["autoStartBreaks"] = True
        self.window._apply_sync(preference_response)

        self.window._issue("start")
        start = self.window.pending[-1]
        running_response = self._bootstrap_response(revision=2)
        running_response["acknowledgements"] = [
            {"commandId": start["id"], "outcome": "applied", "reason": ""}
        ]
        running_response["canonicalTimer"] = dict(self.window.timer)
        running_response["autoStartBreaks"] = True
        self.window._apply_sync(running_response)

        self.window._issue("finish")
        finish = self.window.pending[-1]
        canonical_history = dict(self.window.history[0])
        canonical_history.pop("pending", None)
        completed_response = self._bootstrap_response(
            revision=3, history=[canonical_history]
        )
        completed_response["acknowledgements"] = [
            {"commandId": finish["id"], "outcome": "applied", "reason": ""}
        ]
        completed_response["canonicalTimer"] = dict(self.window.timer)
        completed_response["autoStartBreaks"] = True
        self.cloud.busy = True
        self.window._apply_sync(completed_response)
        self.cloud.busy = False

        self.assertEqual(self.window.timer["status"], "completed")
        with patch(
            "pomodorough.ui.time.monotonic",
            return_value=self.window._auto_break_not_before + 0.1,
        ):
            self.assertTrue(self.window._maybe_auto_start_break())

        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )
        break_starts = [
            command
            for command in self.store.load()["pending"]
            if command["type"] == "start" and command["phase"] == "short_break"
        ]
        self.assertEqual(len(break_starts), 1)
        self.assertEqual(
            self.cloud.payloads[-1]["autoStartOperations"], []
        )

    def test_signed_in_terminal_provisional_break_converges_through_ui_sync(
        self,
    ) -> None:
        self.store.set_user({"id": "user-1"})
        self.store.set_auto_start_breaks(True, now_ms=100)
        terminal = LocalTimer(self.store)
        terminal.issue("start", minutes=1, now_ms=1_000)
        terminal.issue("finish", now_ms=61_000)
        terminal.state(now_ms=61_000)
        pending = self.store.load()["pending"]
        generated = pending[-1]

        self.window._load_state()
        self.window._sync()
        first_request = self.cloud.payloads[-1]
        self.assertEqual(
            [command["type"] for command in first_request["commands"]],
            ["start", "finish"],
        )
        self.assertNotIn(generated, first_request["commands"])

        completed, history = rebuild_optimistic(
            None, [], first_request["commands"]
        )
        for item in history:
            item.pop("pending", None)
        first_response = self._bootstrap_response(revision=1, history=history)
        first_response["acknowledgements"] = [
            {"commandId": command["id"], "outcome": "applied", "reason": ""}
            for command in first_request["commands"]
        ]
        first_response["autoStartAcknowledgements"] = [
            {
                "operationId": operation["id"],
                "outcome": "applied",
                "reason": "",
            }
            for operation in first_request["autoStartOperations"]
        ]
        first_response["canonicalTimer"] = completed
        first_response["autoStartBreaks"] = True

        with patch.object(QMessageBox, "warning"):
            self.window._apply_sync(first_response)

        self.assertEqual(len(self.cloud.payloads), 2)
        second_request = self.cloud.payloads[-1]
        self.assertEqual(len(second_request["commands"]), 1)
        resent = second_request["commands"][0]
        self.assertEqual(resent["id"], generated["id"])
        self.assertEqual(resent["phase"], "short_break")
        running_break, _history = rebuild_optimistic(
            completed, history, second_request["commands"]
        )
        second_response = self._bootstrap_response(revision=2, history=history)
        second_response["acknowledgements"] = [
            {
                "commandId": resent["id"],
                "outcome": "applied",
                "reason": "",
            }
        ]
        second_response["canonicalTimer"] = running_break
        second_response["autoStartBreaks"] = True

        with patch.object(QMessageBox, "warning"):
            self.window._apply_sync(second_response)

        self.assertEqual(self.store.load()["pending"], [])
        self.assertEqual(len(self.cloud.payloads), 2)
        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )

    def test_canonical_long_break_preserves_completed_provisional_short_notification(
        self,
    ) -> None:
        self.store.set_user({"id": "user-1"})
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        provisional = self.store.process_auto_break(
            require_canonical=False, now_ms=3_000
        )[0]
        self.window._load_state()
        self.window._render()
        self.window._sync()
        request = self.window._sync_request
        self.assertIsNotNone(request)

        notifications = []
        with patch.object(
            self.window,
            "_notify",
            side_effect=lambda title, message: notifications.append((title, message)),
        ):
            self.window._issue("finish")
            self.assertEqual(self.window.timer["status"], "completed")
            self.assertEqual(notifications, [])

            canonical_timer, local_history = rebuild_optimistic(
                None, [], request["commands"]
            )
            for item in local_history:
                item.pop("pending", None)
            completed_at = str(local_history[0]["completedAt"])
            history = [
                local_history[0],
                self._history_item("remote-1", completed_at),
                self._history_item("remote-2", completed_at),
                self._history_item("remote-3", completed_at),
            ]
            response = self._bootstrap_response(revision=1, history=history)
            response["acknowledgements"] = [
                {
                    "commandId": command["id"],
                    "outcome": "applied",
                    "reason": "",
                }
                for command in request["commands"]
            ]
            response["autoStartAcknowledgements"] = [
                {
                    "operationId": operation["id"],
                    "outcome": "applied",
                    "reason": "",
                }
                for operation in request["autoStartOperations"]
            ]
            response["canonicalTimer"] = canonical_timer
            response["autoStartBreaks"] = True
            self.window._apply_sync(response)

            self.assertEqual(
                (self.window.timer["id"], self.window.timer["phase"]),
                (provisional["timerId"], "short_break"),
            )
            self.assertEqual(self.window.timer["status"], "completed")
            self.assertEqual(self.window.clock.phase_text, "FOCUS")
            self.assertEqual(self.window.clock.time_text, "25:00")
            self.assertEqual(
                notifications,
                [("Service arrived", "Short break completed.")],
            )

        self.assertEqual(
            notifications,
            [("Service arrived", "Short break completed.")],
        )

    def test_unsigned_offline_provisional_break_notifies_once_on_completion(
        self,
    ) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.store.process_auto_break(require_canonical=False, now_ms=3_000)
        self.window._load_state()
        self.window._render()
        notifications = []

        with patch.object(
            self.window,
            "_notify",
            side_effect=lambda title, message: notifications.append((title, message)),
        ):
            self.window._issue("finish")
            self.window._render()

        self.assertEqual(
            notifications,
            [("Service arrived", "Short break completed.")],
        )

    def test_completion_repeats_sound_until_stop_control_clears_terminal_timer(
        self,
    ) -> None:
        with (
            patch.object(QApplication, "beep") as beep,
            patch.object(self.window, "_issue") as issue,
        ):
            self._queue_completed_timer()

            beep.assert_called_once_with()
            self.assertTrue(self.window.sound_timer.isActive())
            self.assertFalse(self.window.stop_sound_button.isHidden())
            self.assertEqual(self.window.stop_sound_button.text(), "STOP SOUND")

            self.window.stop_sound_button.click()

        self.assertFalse(self.window.sound_timer.isActive())
        self.assertTrue(self.window.stop_sound_button.isHidden())
        issue.assert_called_once_with("clear")

    def test_starting_next_timer_stops_completion_sound(self) -> None:
        with patch.object(QApplication, "beep"):
            self._queue_completed_timer()
            self.assertTrue(self.window.sound_timer.isActive())
            completed_timer_id = self.window.timer["id"]

            self.window._primary_action()

        self.assertEqual(self.window.timer["status"], "running")
        self.assertNotEqual(self.window.timer["id"], completed_timer_id)
        self.assertFalse(self.window.sound_timer.isActive())
        self.assertTrue(self.window.stop_sound_button.isHidden())

    def test_local_focus_completion_waits_before_auto_starting_break(self) -> None:
        self.window.auto_breaks.setChecked(True)
        self.window._issue("start")
        with patch("pomodorough.ui.time.monotonic", return_value=100.0):
            self.window._issue("finish")

        with patch("pomodorough.ui.time.monotonic", return_value=100.25):
            self.window._tick()
        self.assertEqual(self.window.timer["status"], "completed")

        with patch("pomodorough.ui.time.monotonic", return_value=101.2):
            self.window._tick()
        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )

    def test_tick_uses_monotonic_deadline_across_wall_jumps(self) -> None:
        physical_ms = 1_800_000_000_000
        settings = self.store.load()["settings"]
        settings["durationsMs"]["focus"] = 60_000
        with (
            patch("pomodorough.storage.time.time", return_value=physical_ms / 1_000),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=10_000_000_000
            ),
        ):
            self.store.queue_command(
                "start", None, "focus", settings["durationsMs"]
            )
            self.window._load_state()
            self.window._render()

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms + 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=40_000_000_000
            ),
        ):
            self.window._tick()
        self.assertEqual(self.window.timer["status"], "running")
        self.assertEqual(self.window.clock.time_text, "00:30")

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms - 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=70_000_000_000
            ),
        ):
            self.window._tick()
        self.assertEqual(self.window.timer["status"], "completed")

    def test_signed_in_sync_failure_schedules_offline_auto_break(self) -> None:
        self.store.set_user({"id": "user-1"})
        self.store.set_auto_start_breaks(True, now_ms=1)
        self.window._load_state()
        self.window._issue("start")
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.0),
            patch.object(QTimer, "singleShot"),
        ):
            self.window._issue("finish")
        self.assertEqual(self.window.timer["status"], "completed")

        scheduled = []
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.25),
            patch.object(
                QTimer,
                "singleShot",
                side_effect=lambda delay, callback: scheduled.append((delay, callback)),
            ),
        ):
            self.window._cloud_failure("network unavailable")

        self.assertEqual(len(scheduled), 1)
        self.assertGreaterEqual(scheduled[0][0], 950)
        with patch("pomodorough.ui.time.monotonic", return_value=101.2):
            scheduled[0][1]()
        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )

    def test_malformed_finish_response_schedules_offline_auto_break(self) -> None:
        self.store.set_user({"id": "user-1"})
        self.store.set_auto_start_breaks(True, now_ms=1)
        self.window._load_state()
        self.window._issue("start")
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.0),
            patch.object(QTimer, "singleShot"),
        ):
            self.window._issue("finish")

        malformed = self._bootstrap_response(revision=1)
        malformed.pop("canonicalTimer")
        scheduled = []
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.25),
            patch.object(
                QTimer,
                "singleShot",
                side_effect=lambda delay, callback: scheduled.append((delay, callback)),
            ),
            patch.object(QMessageBox, "warning"),
        ):
            self.window._apply_sync(malformed)

        self.assertEqual(len(scheduled), 1)
        self.assertTrue(self.store.has_pending_auto_break())
        with patch("pomodorough.ui.time.monotonic", return_value=101.2):
            scheduled[0][1]()
        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )

    def test_session_expiry_retries_pending_auto_break_offline(self) -> None:
        owner = {"id": "user-1"}
        self.store.set_user(owner)
        self.store.set_auto_start_breaks(True, now_ms=1)
        self.window._load_state()
        self.window._issue("start")
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.0),
            patch.object(QTimer, "singleShot"),
        ):
            self.window._issue("finish")

        scheduled = []
        self.cloud.authenticated = False
        with (
            patch("pomodorough.ui.time.monotonic", return_value=100.25),
            patch.object(
                QTimer,
                "singleShot",
                side_effect=lambda delay, callback: scheduled.append((delay, callback)),
            ),
        ):
            self.window._session_expired()

        self.assertEqual(len(scheduled), 1)
        with patch("pomodorough.ui.time.monotonic", return_value=101.2):
            scheduled[0][1]()

        self.assertEqual(self.window.user, owner)
        self.assertEqual(
            (self.window.timer["phase"], self.window.timer["status"]),
            ("short_break", "running"),
        )
        started = [
            command
            for command in self.store.load()["pending"]
            if command["type"] == "start" and command["phase"] == "short_break"
        ]
        self.assertEqual(len(started), 1)

        self.cloud.authenticated = True
        self.window._signed_in(owner)
        with patch("pomodorough.ui.time.monotonic", return_value=102.0):
            self.window._tick()
        restarted = [
            command
            for command in self.store.load()["pending"]
            if command["type"] == "start" and command["phase"] == "short_break"
        ]
        self.assertEqual(restarted, started)

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
        self.window.revision = 4
        before = len(self.cloud.payloads)

        self.cloud.revision_available.emit(self.window.revision - 1)
        self.assertEqual(len(self.cloud.payloads), before)

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
                "autoStartOperations": [],
                "selectedTaskOperations": [],
            },
        )

    def test_opening_tasks_pulls_without_local_changes(self) -> None:
        before = len(self.cloud.payloads)

        self.window._show_screen(1)

        self.assertEqual(len(self.cloud.payloads), before + 1)

    def test_arrivals_is_separate_tab_next_to_tasks(self) -> None:
        self.assertEqual(
            [button.text() for button in self.window.screen_buttons],
            ["TIMER", "TASKS", "ARRIVALS", "NETWORK"],
        )
        self.assertIs(self.window.page_stack.widget(2), self.window.arrivals_page)
        self.assertTrue(self.window.arrivals_page.isAncestorOf(self.window.history_list))
        self.assertFalse(self.window.right_panel.isAncestorOf(self.window.history_list))

        before = len(self.cloud.payloads)
        self.window._show_screen(2)

        self.assertIs(self.window.page_stack.currentWidget(), self.window.arrivals_page)
        self.assertTrue(self.window.screen_buttons[2].isChecked())
        self.assertEqual(len(self.cloud.payloads), before + 1)

    def test_network_page_reports_unavailable_transport_without_fake_sync(self) -> None:
        iroh = FakeIroh(available=False)
        self.window.quitting = True
        self.window.close()
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.cloud, QIcon(), iroh)

        self.window._show_screen(3)

        self.assertEqual(self.window.screen_buttons[3].text(), "NETWORK")
        self.assertIn("unavailable", self.window.network_unavailable.text().lower())
        self.assertFalse(self.window.iroh_panel.isEnabled())
        self.assertEqual(iroh.started, [])

    def test_network_create_uses_fake_transport_and_exposes_invite(self) -> None:
        iroh = FakeIroh()
        self.window.quitting = True
        self.window.close()
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window = MainWindow(self.store, self.cloud, QIcon(), iroh)
        self.window.room_name_input.setText("Design desk")

        self.window._create_iroh_room()
        iroh.invite_ready.emit("pomodorough1.test")

        self.assertEqual(self.store.replication_mode, "iroh")
        self.assertEqual(iroh.started, [(self.store.active_iroh_room_id, True)])
        self.assertEqual(self.window.invite_output.toPlainText(), "pomodorough1.test")
        self.assertTrue(self.window.copy_invite_button.isEnabled())
        self.assertGreaterEqual(self.cloud.revision_stops, 1)

    def test_synced_task_can_be_selected_while_timer_is_paused(self) -> None:
        task = task_from_title("Remote task")
        self.window._sync()
        self.window._apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [],
                "autoStartAcknowledgements": [],
                "selectedTaskAcknowledgements": [],
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
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "serverTime": utc_timestamp(int(time.time() * 1000)),
                "serverHlcWallMs": int(time.time() * 1000),
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

    def test_remote_task_deletion_does_not_queue_clear_and_keeps_history_title(
        self,
    ) -> None:
        task = task_from_title("Historical task")
        history = self._history_item("remote-completion")
        history["taskId"] = task["id"]
        self.window._sync()
        first = self._bootstrap_response(revision=1, history=[history])
        first["tasks"] = [task]
        self.window._apply_sync(first)
        task_index = self.window.task_combo.findData(task["id"])
        self.window.task_combo.setCurrentIndex(task_index)
        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], task["id"]
        )

        self.window._sync()
        deleted = self._bootstrap_response(revision=2, history=[history])
        deleted["selectedTaskAcknowledgements"] = [
            {
                "operationId": operation["id"],
                "outcome": "applied",
                "reason": "",
            }
            for operation in self.window._sync_request["selectedTaskOperations"]
        ]
        self.window._apply_sync(deleted)

        loaded = self.store.load()
        self.assertIsNone(self.window.settings["selectedTaskId"])
        self.assertIsNone(loaded["settings"]["selectedTaskId"])
        self.assertEqual(loaded["pendingSelectedTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])
        self.assertIn("Focus · Historical task", self.window.history_list.item(0).text())

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
                "autoStartAcknowledgements": [],
                "selectedTaskAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": {
                    "focus": 30 * 60_000,
                    "short_break": 10 * 60_000,
                    "long_break": 20 * 60_000,
                },
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "serverTime": utc_timestamp(int(time.time() * 1000)),
                "serverHlcWallMs": int(time.time() * 1000),
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

        response = {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [
                {
                    "operationId": sent["id"],
                    "outcome": "applied",
                    "reason": "",
                }
            ],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 30 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(int(time.time() * 1000)),
            "serverHlcWallMs": int(time.time() * 1000),
            "serverHlcCounter": 0,
        }
        self.window._apply_sync(response)

        retained = self.store.load()["pendingDurations"]
        self.assertEqual(len(retained), 1)
        self.assertEqual(
            {
                key: value
                for key, value in retained[0].items()
                if key not in {"occurredAt", "hlcWallMs", "hlcCounter"}
            },
            {
                key: value
                for key, value in replacement.items()
                if key not in {"occurredAt", "hlcWallMs", "hlcCounter"}
            },
        )
        self.assertGreater(
            (retained[0]["hlcWallMs"], retained[0]["hlcCounter"]),
            (response["serverHlcWallMs"], response["serverHlcCounter"]),
        )
        self.assertEqual(self.window.duration_spins["focus"].value(), 27)
        self.assertEqual(
            self.cloud.payloads[-1]["durationOperations"], retained
        )
        self.assertFalse(self.window._account_synced)

    def test_cancel_resets_timer_and_preserves_cancelled_history(self) -> None:
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

        self.assertIsNone(self.window.timer)
        self.assertEqual(self.window.clock.time_text, "25:00")
        self.assertEqual(self.window.clock.progress, 0)
        self.assertEqual([item["status"] for item in self.window.history], ["cancelled"])
        self.assertEqual(
            [command["type"] for command in self.store.load()["pending"]],
            ["start", "pause", "cancel", "clear"],
        )


if __name__ == "__main__":
    unittest.main()
