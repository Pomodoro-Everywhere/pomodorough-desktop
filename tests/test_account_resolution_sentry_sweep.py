"""Sentry sweep: account_resolution_controller bare store reads guarded."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pomodorough.account_resolution_controller import (
    AccountResolutionContext,
    AccountResolutionController,
    AccountResolutionPorts,
)
from pomodorough.controller_outcomes import EmitNotice, SetAccountState, Synchronize


class Strings:
    @staticmethod
    def text(key: str, **fields: object) -> str:
        return key + (":" + str(fields) if fields else "")

    @staticmethod
    def plural(key: str, count: int) -> str:
        return f"{key}:{count}"


def _harness() -> tuple[Mock, Mock, AccountResolutionController, list[object]]:
    store = Mock()
    store.pending_resolution.return_value = None
    store.has_sendable_sync_operations.return_value = False
    cloud = Mock(authenticated=True, busy=False, deleting_account=False)
    applied: list[object] = []
    ports = AccountResolutionPorts(
        context=lambda: AccountResolutionContext(store, cloud, Strings(), None),
        apply_outcome=applied.append,
        response_timing=Mock(return_value={}),
        dialog_parent=Mock(return_value=None),  # type: ignore[arg-type]
        present_account=Mock(),
        prompt_history_resolution=Mock(return_value=None),
        confirm_history_resolution=Mock(return_value=False),
        choose_resolution_account_action=Mock(return_value=None),
        choose_account_switch_action=Mock(return_value=None),
        continue_history_resolution=Mock(),
        bootstrap_ready=Mock(),
        signed_in=Mock(),
        clear_sync_request=Mock(),
    )
    return store, cloud, AccountResolutionController(ports), applied


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


def _states(outcome) -> list[bool]:
    return [e.synced for e in outcome.effects if isinstance(e, SetAccountState)]


class ActivatePersistedSweepTests(unittest.TestCase):
    def test_infra_captures_and_blocks(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, _, controller, _ = _harness()
            store.pending_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.activate_persisted_resolution()
            capture.assert_called_once()
            self.assertTrue(outcome.value)
            self.assertTrue(controller.history_resolution_active)
            self.assertTrue(controller.resolution_retry_paused)
            self.assertTrue(_notices(outcome))
            store.pending_resolution.side_effect = None

    def test_validation_stays_silent_but_blocks(self) -> None:
        store, _, controller, _ = _harness()
        store.pending_resolution.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.account_resolution_controller.capture_exception",
        ) as capture:
            outcome = controller.activate_persisted_resolution()
        capture.assert_not_called()
        self.assertTrue(outcome.value)
        self.assertTrue(controller.history_resolution_active)
        self.assertEqual(_notices(outcome), [])


class ContinueSweepTests(unittest.TestCase):
    def _active(self, controller: AccountResolutionController) -> None:
        controller.history_resolution_active = True
        controller.resolution_user = {"id": "user-1"}
        controller.resolution_phase = "resolve"
        controller.resolution_retry_paused = False

    def test_infra_captures_pauses_without_crashing_retry(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, _, controller, _ = _harness()
            self._active(controller)
            store.pending_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.continue_history_resolution()
            capture.assert_called_once()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)
            self.assertEqual(controller.resolution_phase, "resolve")

    def test_validation_stays_silent_pauses(self) -> None:
        store, _, controller, _ = _harness()
        self._active(controller)
        store.pending_resolution.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.account_resolution_controller.capture_exception",
        ) as capture:
            outcome = controller.continue_history_resolution()
        capture.assert_not_called()
        self.assertTrue(_notices(outcome))
        self.assertTrue(controller.resolution_retry_paused)


class ApplySendableSweepTests(unittest.TestCase):
    def _resolved(self, controller: AccountResolutionController) -> None:
        controller.history_resolution_active = True
        controller.resolution_user = {"id": "user-1"}

    def test_infra_captures_falls_back_unsynced(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, _, controller, _ = _harness()
            self._resolved(controller)
            store.apply_resolution.return_value = []
            store.has_sendable_sync_operations.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.apply_resolution({})
            capture.assert_called_once()
            self.assertIn(False, _states(outcome))
            self.assertTrue(_notices(outcome))
            self.assertNotIn(
                Synchronize, tuple(map(type, outcome.effects))
            )

    def test_validation_stays_silent_falls_back_unsynced(self) -> None:
        store, _, controller, _ = _harness()
        self._resolved(controller)
        store.apply_resolution.return_value = []
        store.has_sendable_sync_operations.side_effect = ValueError("bad")
        with patch(
            "pomodorough.account_resolution_controller.capture_exception",
        ) as capture:
            outcome = controller.apply_resolution({})
        capture.assert_not_called()
        self.assertIn(False, _states(outcome))
        self.assertTrue(_notices(outcome))


class SignedInSweepTests(unittest.TestCase):
    def test_infra_captures_and_enters_corruption_without_login_crash(
        self,
    ) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, _, controller, _ = _harness()
            store.pending_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.signed_in({"id": "user-1"})
            capture.assert_called_once()
            self.assertTrue(controller.history_resolution_active)
            self.assertTrue(controller.resolution_retry_paused)
            self.assertIn(False, _states(outcome))
            self.assertTrue(_notices(outcome))

    def test_validation_stays_silent_but_notices(self) -> None:
        store, _, controller, _ = _harness()
        store.pending_resolution.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.account_resolution_controller.capture_exception",
        ) as capture:
            outcome = controller.signed_in({"id": "user-1"})
        capture.assert_not_called()
        self.assertTrue(_notices(outcome))
        self.assertTrue(controller.history_resolution_active)


class SweepCommentTests(unittest.TestCase):
    def test_validation_comments_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "pomodorough"
        text = (root / "account_resolution_controller.py").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(text.count("# validation stays silent"), 4)


if __name__ == "__main__":
    unittest.main()
