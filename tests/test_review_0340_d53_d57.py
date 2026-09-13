"""0.34.0 review D53-D57: resolution infra split, TUI validation, helper
contract, suffixed scrub forms, device_id guard."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pomodorough.account_resolution_controller import (
    AccountResolutionContext,
    AccountResolutionController,
    AccountResolutionPorts,
)
from pomodorough.controller_outcomes import EmitNotice
from pomodorough.sentry_monitoring import scrub_sentry_event
from pomodorough.storage import Store


class Strings:
    @staticmethod
    def text(key: str, **fields: object) -> str:
        return key + (":" + str(fields) if fields else "")

    @staticmethod
    def plural(key: str, count: int) -> str:
        return f"{key}:{count}"


def _resolution_harness() -> tuple[Mock, AccountResolutionController]:
    store = Mock()
    store.pending_resolution.return_value = None
    store.has_sendable_sync_operations.return_value = False
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
    return store, AccountResolutionController(ports)


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


def _activate(controller: AccountResolutionController) -> None:
    controller.history_resolution_active = True
    controller.resolution_user = {"id": "user-1"}
    controller.resolution_phase = "preview"


class D53BootstrapPlanSplitTests(unittest.TestCase):
    def test_infra_captures_pauses_with_notice(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, controller = _resolution_harness()
            _activate(controller)
            store.bootstrap_resolution_plan.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_ready({})
            capture.assert_called_once()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)
            self.assertEqual(controller.resolution_phase, "preview")

    def test_validation_stays_silent_pauses_with_notice(self) -> None:
        for error in (KeyError("k"), TypeError("t"), ValueError("v")):
            store, controller = _resolution_harness()
            _activate(controller)
            store.bootstrap_resolution_plan.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_ready({})
            capture.assert_not_called()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)


class D53PrepareResolutionSplitTests(unittest.TestCase):
    def _planned(self) -> tuple[Mock, AccountResolutionController]:
        store, controller = _resolution_harness()
        _activate(controller)
        store.bootstrap_resolution_plan.return_value = {
            "strategy": "merge",
            "expectedRevision": 3,
        }
        return store, controller

    def test_infra_captures_pauses_with_notice(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, controller = self._planned()
            store.prepare_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_ready({})
            capture.assert_called_once()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)

    def test_validation_stays_silent_pauses_with_notice(self) -> None:
        for error in (KeyError("k"), TypeError("t"), ValueError("v")):
            store, controller = self._planned()
            store.prepare_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.bootstrap_ready({})
            capture.assert_not_called()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)


class D53ApplyResolutionSplitTests(unittest.TestCase):
    def test_infra_captures_pauses_with_notice(self) -> None:
        for error in (OSError("io"), sqlite3.Error("db")):
            store, controller = _resolution_harness()
            _activate(controller)
            store.apply_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.apply_resolution({})
            capture.assert_called_once()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)

    def test_validation_stays_silent_pauses_with_notice(self) -> None:
        for error in (KeyError("k"), TypeError("t"), ValueError("v")):
            store, controller = _resolution_harness()
            _activate(controller)
            store.apply_resolution.side_effect = error
            with patch(
                "pomodorough.account_resolution_controller.capture_exception",
            ) as capture:
                outcome = controller.apply_resolution({})
            capture.assert_not_called()
            self.assertTrue(_notices(outcome))
            self.assertTrue(controller.resolution_retry_paused)


class D54TuiValidationLoopTests(unittest.TestCase):
    def test_shape_errors_keep_loop_alive_without_capture(self) -> None:
        from pomodorough import tui

        for error in (
            ValueError("corrupt settings"),
            KeyError("durationsMs"),
            TypeError("bad snapshot"),
        ):
            timer = Mock()
            screen = Mock()
            screen.getmaxyx.return_value = (24, 80)
            screen.getch.return_value = ord("q")
            draws = [error, None]
            with patch.object(
                tui, "_draw", side_effect=lambda *a: self._pop_draw(draws),
            ):
                with patch(
                    "pomodorough.tui.capture_exception",
                ) as capture:
                    tui._run(screen, timer)
            capture.assert_not_called()

    @staticmethod
    def _pop_draw(draws: list) -> None:
        outcome = draws.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def test_json_decode_survives_as_validation(self) -> None:
        from pomodorough import tui

        timer = Mock()
        screen = Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.getch.return_value = ord("q")
        corrupt = json.JSONDecodeError("bad persisted JSON", "{", 1)
        draws: list = [corrupt, None]
        with patch.object(
            tui, "_draw", side_effect=lambda *a: self._pop_draw(draws),
        ):
            with patch(
                "pomodorough.tui.capture_exception",
            ) as capture:
                tui._run(screen, timer)
        capture.assert_not_called()


class D55HelperContractTests(unittest.TestCase):
    def test_contract_comment_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "pomodorough"
        text = (root / "terminal.py").read_text(encoding="utf-8")
        self.assertIn("D55", text)
        self.assertIn("cli._run_with_store", text)
        self.assertIn("tui._run", text)

    def test_store_action_bubbles_infra_without_capture(self) -> None:
        from pomodorough.terminal import LocalTimer

        timer = LocalTimer.__new__(LocalTimer)
        timer.store = Mock()
        for error in (OSError("io"), sqlite3.Error("db")):
            def failing() -> None:
                raise error

            with patch(
                "pomodorough.terminal.capture_exception",
            ) as capture:
                with self.assertRaises(type(error)):
                    timer._store_action(failing)
            capture.assert_not_called()

    def test_pending_auto_break_bubbles_infra_without_capture(self) -> None:
        from pomodorough.terminal import LocalTimer

        for error in (OSError("io"), sqlite3.Error("db")):
            timer = LocalTimer.__new__(LocalTimer)
            timer.store = Mock()
            timer.resolution_pending = False
            timer.store.has_pending_auto_break.side_effect = error
            with patch(
                "pomodorough.terminal.capture_exception",
            ) as capture:
                with self.assertRaises(type(error)):
                    timer._process_pending_auto_break(False, 0, 0)
            capture.assert_not_called()


# D56: suffixed free-text/JSON-string forms the bare alternation missed.
D56_SUFFIXED_PARAMS = (
    "room_id", "roomId", "device_id", "deviceId",
    "peer_id", "peerId", "endpoint_url", "endpointUrl",
    "ticket_code", "ticketCode", "invite_code", "inviteCode",
)

# Pre-D56 alternation tail (bare names only): proves the leak existed.
_OLD_TOKEN_TAIL = (
    r"sessionid|sid|ssid|room|endpoint|device|peer|ticket|invite"
)
_OLD_TOKEN_PARAM_RE = re.compile(
    r"(?i)([?&#](?:" + _OLD_TOKEN_TAIL + r")=)[^&\s\"';]+"
)
_OLD_JSON_SECRET_RE = re.compile(
    r"(?i)([\"'](?:" + _OLD_TOKEN_TAIL + r")[\"']\s*:\s*[\"'])[^\"']+"
)


class D56SuffixedScrubTests(unittest.TestCase):
    def test_url_params_are_stripped(self) -> None:
        for name in D56_SUFFIXED_PARAMS:
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
        for name in D56_SUFFIXED_PARAMS:
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

    def test_leak_before_proven_on_old_pattern(self) -> None:
        for name in D56_SUFFIXED_PARAMS:
            with self.subTest(name=name):
                self.assertIsNone(
                    _OLD_TOKEN_PARAM_RE.search(f"?{name}=secret-value"),
                    f"old pattern unexpectedly covers {name}",
                )
                self.assertIsNone(
                    _OLD_JSON_SECRET_RE.search(f'"{name}":"secret-value"'),
                    f"old JSON pattern unexpectedly covers {name}",
                )

    def test_benign_params_stay(self) -> None:
        rendered = json.dumps(
            scrub_sentry_event({"message": "GET https://x/cb?next=1 ok"}, None)
        )
        self.assertIn("next=1", rendered)


class D57DeviceIdGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001 - closed twice stays quiet.
            pass
        self.temporary.cleanup()

    def test_infra_bubbles_without_internal_capture(self) -> None:
        self.store.close()
        with patch(
            "pomodorough.storage.capture_exception",
        ) as capture:
            with self.assertRaises(sqlite3.Error):
                _ = self.store.device_id
        capture.assert_not_called()

    def test_validation_stays_silent_but_raises(self) -> None:
        with patch.object(
            self.store, "get_meta", side_effect=ValueError("corrupt"),
        ):
            with patch(
                "pomodorough.storage.capture_exception",
            ) as capture:
                with self.assertRaises(ValueError):
                    _ = self.store.device_id
        capture.assert_not_called()

    def test_pending_start_reports_exactly_once_across_namespaces(self) -> None:
        from types import SimpleNamespace

        from pomodorough import cli as cli_module

        real_get_meta = self.store.get_meta

        def get_meta(key, default=None):
            if key == "deviceId":
                raise sqlite3.Error("device identity unreadable")
            return real_get_meta(key, default)

        args = SimpleNamespace(
            command="start", phase=None, minutes=None, as_json=True,
        )
        with patch.object(self.store, "get_meta", side_effect=get_meta):
            with patch(
                "pomodorough.storage.capture_exception",
            ) as storage_capture:
                with patch(
                    "pomodorough.cli.capture_exception",
                ) as boundary_capture:
                    error = cli_module._run_with_store(
                        args, self.store, Mock(), Strings(),
                    )
        self.assertIsInstance(error, sqlite3.Error)
        storage_capture.assert_not_called()
        boundary_capture.assert_called_once()
        total = storage_capture.call_count + boundary_capture.call_count
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
