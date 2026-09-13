"""0.35.0 review D58-D62: invite scrub, CLI validation, app single-capture,
conflict discard split, TUI startup boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.account_resolution_controller import (
    AccountResolutionContext,
    AccountResolutionController,
    AccountResolutionPorts,
)
from pomodorough.controller_outcomes import EmitNotice
from pomodorough.sentry_monitoring import scrub_sentry_event


class Strings:
    @staticmethod
    def text(key: str, **fields: object) -> str:
        return key + (":" + str(fields) if fields else "")

    @staticmethod
    def plural(key: str, count: int) -> str:
        return f"{key}:{count}"


# D58: invite fields the D51/D56 alternation missed (camel + snake + flat).
D58_URL_NAMES = (
    "roomSecret",
    "room_secret",
    "roomsecret",
    "roomName",
    "room_name",
    "roomname",
    "endpointTicket",
    "endpoint_ticket",
    "endpointticket",
    "endpointId",
    "endpoint_id",
    "endpointid",
)


class D58InviteScrubTests(unittest.TestCase):
    def test_poc_pair_filtered(self) -> None:
        url = "https://x/cb?roomSecret=ABC&next=1"
        rendered = json.dumps(scrub_sentry_event({"message": url}, None))
        self.assertNotIn("ABC", rendered)
        self.assertIn("[Filtered]", rendered)
        payload = '{"roomSecret":"super-secret-value"}'
        rendered = json.dumps(scrub_sentry_event({"message": payload}, None))
        self.assertNotIn("super-secret-value", rendered)
        self.assertIn("[Filtered]", rendered)

    def test_url_params_are_stripped(self) -> None:
        for name in D58_URL_NAMES:
            urls = (
                f"https://x/cb?{name}=secret-{name}-1&next=1",
                f"https://x/cb?next=1&{name}=secret-{name}-2",
                f"https://x/cb#{name}=secret-{name}-3",
            )
            for url in urls:
                with self.subTest(name=name, url=url):
                    rendered = json.dumps(
                        scrub_sentry_event({"message": url}, None)
                    )
                    self.assertNotIn(f"secret-{name}", rendered)
                    self.assertIn("[Filtered]", rendered)

    def test_json_string_secrets_are_stripped(self) -> None:
        for name in D58_URL_NAMES:
            payloads = (
                '{"' + name + '":"json-secret-1"}',
                "{'" + name + "' : 'json-secret-2'}",
            )
            for payload in payloads:
                with self.subTest(name=name, payload=payload):
                    rendered = json.dumps(
                        scrub_sentry_event({"message": payload}, None)
                    )
                    self.assertNotIn("json-secret", rendered)
                    self.assertIn("[Filtered]", rendered)

    def test_benign_params_stay(self) -> None:
        rendered = json.dumps(
            scrub_sentry_event({"message": "GET https://x/cb?next=1 ok"}, None)
        )
        self.assertIn("next=1", rendered)


class D59CliValidationTests(unittest.TestCase):
    def test_validation_triple_returns_silently_without_capture(self) -> None:
        from pomodorough import cli as cli_module

        errors = (
            KeyError("bad key"),
            TypeError("bad type"),
            ValueError("corrupt settings"),
            json.JSONDecodeError("bad persisted JSON", "{", 1),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(cli_module, "LocalTimer"):
                    with patch.object(
                        cli_module, "run", side_effect=error
                    ):
                        with patch(
                            "pomodorough.cli.capture_exception",
                        ) as capture:
                            result = cli_module._run_with_store(
                                Mock(), Mock(), Mock(), Strings(),
                            )
                self.assertIs(result, error)
                capture.assert_not_called()

    def test_infra_still_captures(self) -> None:
        from pomodorough import cli as cli_module

        for error in (OSError("io"), sqlite3.Error("db")):
            with self.subTest(error=type(error).__name__):
                with patch.object(cli_module, "LocalTimer"):
                    with patch.object(
                        cli_module, "run", side_effect=error
                    ):
                        with patch(
                            "pomodorough.cli.capture_exception",
                        ) as capture:
                            result = cli_module._run_with_store(
                                Mock(), Mock(), Mock(), Strings(),
                            )
                self.assertIs(result, error)
                capture.assert_called_once_with(error)


def _run_app_main_with_store_failure(error: Exception) -> tuple[int, Mock, Mock]:
    from pomodorough import app as app_module

    application = Mock()
    lock = Mock()
    lock.tryLock.return_value = True
    with (
        patch.object(
            app_module, "init_sentry_from_environment", return_value=False
        ),
        patch.object(app_module, "QApplication", return_value=application),
        patch.object(app_module, "QIcon"),
        patch.object(app_module, "_instance_lock", return_value=lock),
        patch.object(app_module, "Store", side_effect=error),
        patch.object(app_module, "CloudService"),
        patch.object(app_module, "_iroh_service"),
        patch.object(app_module, "MainWindow"),
        patch.object(app_module, "capture_exception") as app_capture,
        patch(
            "pomodorough.sentry_monitoring.capture_exception",
        ) as sentry_capture,
    ):
        result = app_module.main()
    return result, app_capture, sentry_capture


class D60AppSingleCaptureTests(unittest.TestCase):
    def test_startup_infra_returns_1_with_exactly_one_capture(self) -> None:
        for error in (OSError("disk gone"), sqlite3.Error("db gone")):
            with self.subTest(error=type(error).__name__):
                result, app_capture, sentry_capture = (
                    _run_app_main_with_store_failure(error)
                )
                self.assertEqual(result, 1)
                app_capture.assert_called_once()
                sentry_capture.assert_not_called()
                total = app_capture.call_count + sentry_capture.call_count
                self.assertEqual(total, 1)


def _conflict_harness() -> tuple[Mock, AccountResolutionController]:
    store = Mock()
    store.discard_pending_resolution.return_value = True
    cloud = Mock(authenticated=True, busy=False, deleting_account=False)
    ports = AccountResolutionPorts(
        context=lambda: AccountResolutionContext(store, cloud, Strings(), None),
        apply_outcome=Mock(),
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
    controller = AccountResolutionController(ports)
    controller.history_resolution_active = True
    controller.resolution_user = {"id": "user-1"}
    controller.resolution_request_id = "req-1"
    return store, controller


def _conflict_notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


class D61BootstrapConflictSplitTests(unittest.TestCase):
    def test_infra_captures_pauses_with_notice(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, controller = _conflict_harness()
            store.discard_pending_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_conflict({})
            capture.assert_called_once_with(error)
            self.assertTrue(_conflict_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)

    def test_validation_stays_silent_pauses_with_notice(self) -> None:
        for error in (KeyError("k"), TypeError("t"), ValueError("v")):
            store, controller = _conflict_harness()
            store.discard_pending_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_conflict({})
            capture.assert_not_called()
            self.assertTrue(_conflict_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)

    def test_false_return_keeps_discard_failed_notice(self) -> None:
        store, controller = _conflict_harness()
        store.discard_pending_resolution.return_value = False
        with patch(
            "pomodorough.account_resolution_controller.capture_exception",
        ) as capture:
            outcome = controller.bootstrap_conflict({})
        capture.assert_not_called()
        self.assertTrue(_conflict_notices(outcome))
        self.assertTrue(controller.resolution_retry_paused)

    def test_success_clears_and_continues(self) -> None:
        store, controller = _conflict_harness()
        outcome = controller.bootstrap_conflict({"message": "changed"})
        self.assertEqual(outcome.effects, ())
        self.assertFalse(controller.resolution_retry_paused)
        self.assertEqual(controller.resolution_phase, "preview")
        controller._ports.continue_history_resolution.assert_called_once()


class D62TuiStartupBoundaryTests(unittest.TestCase):
    def test_store_infra_returns_2_with_capture(self) -> None:
        from pomodorough import tui as tui_module

        for error in (OSError("disk gone"), sqlite3.Error("db gone")):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(
                        tui_module, "Store", side_effect=error
                    ) as store_type,
                    patch.object(tui_module, "LocalTimer") as timer_type,
                    patch.object(tui_module.curses, "wrapper") as wrapper,
                    patch(
                        "pomodorough.tui.capture_exception",
                    ) as capture,
                ):
                    result = tui_module.main([])
                self.assertEqual(result, 2)
                capture.assert_called_once_with(error)
                store_type.assert_called_once()
                timer_type.assert_not_called()
                wrapper.assert_not_called()

    def test_store_failure_never_reaches_traceback(self) -> None:
        from pomodorough import tui as tui_module

        with (
            patch.object(
                tui_module, "Store", side_effect=OSError("no store")
            ),
            patch.object(tui_module, "LocalTimer") as timer_type,
            patch.object(tui_module.curses, "wrapper") as wrapper,
            patch("pomodorough.tui.capture_exception"),
        ):
            try:
                result = tui_module.main([])
            except OSError:
                self.fail("Store() escaped the startup boundary")
        self.assertEqual(result, 2)
        timer_type.assert_not_called()
        wrapper.assert_not_called()

    def test_success_path_still_closes_store(self) -> None:
        from pomodorough import tui as tui_module

        store = Mock()
        with (
            patch.object(tui_module, "Store", return_value=store),
            patch.object(tui_module, "LocalTimer", return_value=Mock()),
            patch.object(tui_module.curses, "wrapper", return_value=None),
        ):
            result = tui_module.main([])
        self.assertEqual(result, 0)
        store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
