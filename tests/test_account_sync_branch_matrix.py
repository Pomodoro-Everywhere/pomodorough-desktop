from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QMessageBox

from pomodorough.account_resolution_controller import (
    AccountResolutionContext,
    AccountResolutionController,
    AccountResolutionPorts,
)
from pomodorough.controller_outcomes import EmitNotice, Synchronize
from pomodorough.synchronization_controller import (
    SynchronizationContext,
    SynchronizationController,
    SynchronizationPorts,
)


class Strings:
    @staticmethod
    def text(key: str, **fields: object) -> str:
        return key + (":" + str(fields) if fields else "")

    @staticmethod
    def plural(key: str, count: int) -> str:
        return f"{key}:{count}"


class AccountHarness:
    def __init__(self) -> None:
        self.store = Mock()
        self.store.pending_resolution.return_value = None
        self.store.has_sendable_sync_operations.return_value = False
        self.store.load.return_value = {key: [] for key in ("pending", "pendingTasks", "pendingDurations", "pendingAutoStarts", "pendingSelectedTasks")}
        self.cloud = Mock(authenticated=True, busy=False, deleting_account=False)
        self.user = None
        self.applied: list[object] = []
        self.presentations: list[object] = []
        self.ports = AccountResolutionPorts(
            context=self.context,
            apply_outcome=self.applied.append,
            response_timing=Mock(return_value={}),
            dialog_parent=Mock(return_value=None),  # type: ignore[arg-type]
            present_account=self.presentations.append,
            prompt_history_resolution=Mock(return_value=None),
            confirm_history_resolution=Mock(return_value=False),
            choose_resolution_account_action=Mock(return_value=None),
            choose_account_switch_action=Mock(return_value=None),
            continue_history_resolution=Mock(),
            bootstrap_ready=Mock(),
            signed_in=Mock(),
            clear_sync_request=Mock(),
        )
        self.controller = AccountResolutionController(self.ports)

    def context(self) -> AccountResolutionContext:
        return AccountResolutionContext(self.store, self.cloud, Strings(), self.user)


class AccountResolutionBranchMatrixTests(unittest.TestCase):
    def test_retry_and_resume_cover_duplicate_inactive_choice_and_continue(self) -> None:
        harness = AccountHarness()
        controller = harness.controller
        with patch("pomodorough.account_resolution_controller.QTimer.singleShot") as shot:
            controller.schedule_resolution_retry()
            controller.schedule_resolution_retry()
        shot.assert_called_once()
        self.assertEqual(controller.resume_history_resolution().effects, ())
        controller.history_resolution_active = True
        controller.resolution_user = {"id": "user"}
        controller.resolution_phase = "choice"
        controller.resolution_preview = {"revision": 1}
        controller.resume_history_resolution()
        harness.ports.bootstrap_ready.assert_called_once_with({"revision": 1})
        controller.resolution_phase = "preview"
        controller.resume_history_resolution()
        harness.cloud.preview_bootstrap.assert_called_once()

    def test_continue_resolution_waits_for_busy_then_resolves_or_previews(self) -> None:
        harness = AccountHarness()
        controller = harness.controller
        controller.history_resolution_active = True
        controller.resolution_user = {"id": "user"}
        controller.resolution_phase = "resolve"
        harness.cloud.busy = True
        with patch("pomodorough.account_resolution_controller.QTimer.singleShot"):
            controller.continue_history_resolution()
        self.assertTrue(controller.resolution_retry_scheduled)
        harness.cloud.busy = False
        controller.resolution_retry_scheduled = False
        harness.store.pending_resolution.return_value = None
        controller.continue_history_resolution()
        harness.cloud.preview_bootstrap.assert_called_once()
        harness.store.pending_resolution.return_value = {"request": {"requestId": "request"}}
        controller.resolution_phase = "resolve"
        controller.continue_history_resolution()
        harness.cloud.resolve_bootstrap.assert_called_once()

    def test_apply_resolution_covers_inactive_failure_pending_sync_and_notices(self) -> None:
        harness = AccountHarness()
        controller = harness.controller
        self.assertEqual(controller.apply_resolution({}).effects, ())
        controller.history_resolution_active = True
        controller.resolution_user = {"id": "user"}
        harness.store.apply_resolution.side_effect = ValueError("invalid")
        self.assertIsInstance(controller.apply_resolution({}).effects[0], EmitNotice)
        harness.store.apply_resolution.side_effect = None
        harness.store.apply_resolution.return_value = ["conflict"]
        harness.store.has_sendable_sync_operations.return_value = True
        outcome = controller.apply_resolution({})
        self.assertIn(Synchronize, tuple(map(type, outcome.effects)))
        self.assertIsInstance(outcome.effects[-1], EmitNotice)

    def test_bootstrap_conflict_rejects_missing_identity_and_pauses_retry(self) -> None:
        harness = AccountHarness()
        controller = harness.controller
        self.assertEqual(controller.bootstrap_conflict({}).effects, ())
        controller.history_resolution_active = True
        controller.resolution_user = {}
        controller.resolution_request_id = None
        outcome = controller.bootstrap_conflict({})
        self.assertIsInstance(outcome.effects[0], EmitNotice)
        self.assertTrue(controller.resolution_retry_paused)

    def test_delete_account_covers_auth_busy_cancel_mismatch_and_confirmation(self) -> None:
        harness = AccountHarness()
        harness.cloud.authenticated = False
        self.assertEqual(harness.controller.delete_account_action().effects, ())
        harness.cloud.authenticated = True
        harness.cloud.deleting_account = True
        self.assertEqual(harness.controller.delete_account_action().effects, ())
        harness.cloud.deleting_account = False
        with patch("pomodorough.account_resolution_controller.QInputDialog.getText", return_value=("", False)):
            self.assertEqual(harness.controller.delete_account_action().effects, ())
        with patch("pomodorough.account_resolution_controller.QInputDialog.getText", return_value=("wrong", True)):
            self.assertEqual(harness.controller.delete_account_action().effects[0].duration_ms, 10_000)
        with patch("pomodorough.account_resolution_controller.QInputDialog.getText", return_value=("DELETE", True)):
            harness.controller.delete_account_action()
        harness.cloud.delete_account.assert_called_once_with("DELETE")

    def test_account_action_covers_switch_signout_resolution_and_login(self) -> None:
        harness = AccountHarness()
        controller = harness.controller
        controller.account_switch_user = {"id": "new"}
        harness.ports.choose_account_switch_action.return_value = "sign_out"
        controller.account_action()
        harness.cloud.logout.assert_called_once()
        controller.account_switch_user = None
        controller.history_resolution_active = True
        harness.ports.choose_resolution_account_action.return_value = "sign_out"
        controller.account_action()
        self.assertEqual(harness.cloud.logout.call_count, 2)
        controller.history_resolution_active = False
        with patch("pomodorough.account_resolution_controller.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            controller.account_action()
        self.assertEqual(harness.cloud.logout.call_count, 3)
        harness.cloud.authenticated = False
        controller.account_action()
        harness.cloud.login.assert_called_once()


class SyncHarness:
    def __init__(self) -> None:
        self.store = Mock()
        self.store.pending_resolution.return_value = None
        self.store.sync_payload.return_value = {key: [] for key in ("commands", "taskOperations", "durationOperations", "autoStartOperations", "selectedTaskOperations")}
        self.store.has_sendable_sync_operations.return_value = False
        self.cloud = Mock(authenticated=True, busy=False)
        self.iroh = Mock()
        self.closed = False
        self.revision = 1
        self.mode = "centralized"
        self.join_pending = False
        self.resolving = False
        self.applied: list[object] = []
        self.ports = SynchronizationPorts(
            context=self.context,
            apply_outcome=self.applied.append,
            response_timing=Mock(return_value={}),
            activate_persisted_resolution=Mock(return_value=True),
            continue_history_resolution=Mock(),
            retry_sync=Mock(),
            synchronize=Mock(),
            iroh_failure=Mock(),
        )
        self.controller = SynchronizationController(self.ports)

    def context(self) -> SynchronizationContext:
        return SynchronizationContext(self.store, self.cloud, self.iroh, Strings(), self.closed, self.revision, self.mode, self.join_pending, self.resolving)


class SynchronizationBranchMatrixTests(unittest.TestCase):
    def test_sync_routes_closed_offline_and_iroh_success_failure_and_absent_service(self) -> None:
        harness = SyncHarness()
        harness.closed = True
        self.assertEqual(harness.controller.sync().effects, ())
        harness.closed = False
        harness.mode = "offline"
        self.assertEqual(harness.controller.sync().effects, ())
        harness.mode = "iroh"
        harness.store.capture_local_iroh_records.return_value = True
        harness.controller.sync()
        self.assertEqual(len(harness.applied), 1)
        harness.iroh = None
        harness.store.capture_local_iroh_records.return_value = False
        harness.controller.sync()
        harness.store.capture_local_iroh_records.side_effect = OSError("capture")
        harness.controller.sync()
        harness.ports.iroh_failure.assert_called_once_with("capture")

    def test_centralized_busy_retry_and_resolution_paths(self) -> None:
        harness = SyncHarness()
        harness.store.pending_resolution.return_value = {"request": {}}
        harness.resolving = True
        harness.controller.sync()
        harness.ports.continue_history_resolution.assert_called_once()
        harness.resolving = False
        harness.store.pending_resolution.return_value = None
        harness.cloud.busy = True
        with patch("pomodorough.synchronization_controller.QTimer.singleShot"):
            harness.controller.sync()
            harness.controller.sync()
        self.assertTrue(harness.controller.sync_waiting)

    def test_apply_sync_covers_stale_failure_pending_and_notice_paths(self) -> None:
        harness = SyncHarness()
        self.assertIsInstance(harness.controller.apply_sync({}).effects[1], EmitNotice)
        harness.controller.sync_request = {"commands": []}
        harness.store.apply_sync.side_effect = ValueError("invalid")
        self.assertIsInstance(harness.controller.apply_sync({}).effects[0], EmitNotice)
        harness.store.apply_sync.side_effect = None
        harness.store.apply_sync.return_value = ["conflict"]
        harness.store.has_sendable_sync_operations.return_value = True
        harness.controller.sync_request = {"commands": []}
        outcome = harness.controller.apply_sync({})
        self.assertIn(Synchronize, tuple(map(type, outcome.effects)))
        self.assertIsInstance(outcome.effects[-1], EmitNotice)

    def test_cloud_failure_distinguishes_authentication_message_and_session_state(self) -> None:
        harness = SyncHarness()
        signed_in = harness.controller.cloud_failure("network failed")
        self.assertEqual(len(signed_in.effects), 3)
        sign_in = harness.controller.cloud_failure("Sign in to sync")
        self.assertEqual(len(sign_in.effects), 2)
        harness.cloud.authenticated = False
        signed_out = harness.controller.cloud_failure("offline")
        self.assertEqual(len(signed_out.effects), 2)


if __name__ == "__main__":
    unittest.main()
