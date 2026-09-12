"""0.33.0 review D49-D52: sqlite guards, bare-read hardening, scrub, silence."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough.controller_outcomes import EmitNotice, SetAccountState
from pomodorough.synchronization_controller import (
    SynchronizationContext,
    SynchronizationController,
    SynchronizationPorts,
)
from pomodorough.sentry_monitoring import scrub_sentry_event
from pomodorough.terminal import InvalidAction


def _sync_harness(mode: str = "centralized", resolving: bool = False) -> SimpleNamespace:
    store = Mock()
    store.pending_resolution.return_value = None
    store.sync_payload.return_value = {
        key: [] for key in (
            "commands", "taskOperations", "durationOperations",
            "autoStartOperations", "selectedTaskOperations",
        )
    }
    store.has_sendable_sync_operations.return_value = False
    cloud = Mock(authenticated=True, busy=False)
    applied: list = []
    ports = SynchronizationPorts(
        context=lambda: SynchronizationContext(
            store, cloud, Mock(), Mock(text=lambda k, **f: k),
            False, 1, mode, False, resolving,
        ),
        apply_outcome=applied.append,
        response_timing=Mock(return_value={}),
        activate_persisted_resolution=Mock(),
        continue_history_resolution=Mock(),
        retry_sync=Mock(),
        synchronize=Mock(),
        iroh_failure=Mock(),
    )
    return SimpleNamespace(
        store=store, cloud=cloud, ports=ports,
        controller=SynchronizationController(ports), applied=applied,
    )


def _notices(outcome) -> list[str]:
    return [e.message for e in outcome.effects if isinstance(e, EmitNotice)]


class D49TuiSplitTests(unittest.TestCase):
    def test_sqlite_error_captures_and_stays_in_loop(self) -> None:
        from pomodorough import tui

        timer = Mock()
        screen = Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.getch.side_effect = [ord(" "), ord("q")]

        def fake_key(timer_arg, key_arg) -> bool:
            if key_arg == ord(" "):
                raise sqlite3.Error("db boom")
            return False

        with patch.object(tui, "_draw", return_value=None):
            with patch.object(tui, "handle_key", side_effect=fake_key):
                with patch(
                    "pomodorough.tui.capture_exception",
                ) as capture:
                    tui._run(screen, timer)
        capture.assert_called_once()

    def test_invalid_action_stays_silent(self) -> None:
        from pomodorough import tui

        timer = Mock()
        screen = Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.getch.side_effect = [ord(" "), ord("q")]

        def fake_key(timer_arg, key_arg) -> bool:
            if key_arg == ord(" "):
                raise InvalidAction("bad key")
            return False

        with patch.object(tui, "_draw", return_value=None):
            with patch.object(tui, "handle_key", side_effect=fake_key):
                with patch(
                    "pomodorough.tui.capture_exception",
                ) as capture:
                    tui._run(screen, timer)
        capture.assert_not_called()


class D49SyncSplitTests(unittest.TestCase):
    def test_sync_iroh_splits_infra_and_validation(self) -> None:
        harness = _sync_harness(mode="iroh")
        for error in (OSError("io"), sqlite3.Error("db")):
            harness.store.capture_local_iroh_records.side_effect = error
            with patch(
                "pomodorough.synchronization_controller.capture_exception",
            ) as capture:
                harness.controller.sync()
            capture.assert_called_once()
        harness.store.capture_local_iroh_records.side_effect = ValueError("bad")
        with patch(
            "pomodorough.synchronization_controller.capture_exception",
        ) as capture:
            harness.controller.sync()
        capture.assert_not_called()

    def test_apply_sync_infra_captures_and_fails(self) -> None:
        harness = _sync_harness()
        harness.controller.sync_request = {"commands": []}
        harness.store.apply_sync.side_effect = sqlite3.Error("db boom")
        with patch(
            "pomodorough.synchronization_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.apply_sync({})
        capture.assert_called_once()
        self.assertTrue(_notices(outcome))


class D50BareReadTests(unittest.TestCase):
    def test_newly_activated_resolution_blocks_current_mutation(self) -> None:
        from pomodorough.ui_controller import ApplicationController

        resolution = SimpleNamespace(history_resolution_active=False)
        controller = SimpleNamespace(
            replication=SimpleNamespace(iroh_join_pending=False),
            account_resolution=resolution,
            store=Mock(), notice=Mock(), strings=Mock(text=lambda key: key),
            _activate_persisted_resolution=Mock(
                side_effect=lambda: setattr(resolution, "history_resolution_active", True)
            ),
            _render=Mock(), _set_account_state=Mock(),
        )
        controller.store.pending_resolution.return_value = {"requestId": "pending"}

        self.assertTrue(ApplicationController._mutation_blocked(controller))

        controller._activate_persisted_resolution.assert_called_once_with()
        controller._set_account_state.assert_called_once_with(False)
        controller.notice.emit.assert_called_once_with("resolution.blocked")

    def test_centralized_pending_and_payload_guarded(self) -> None:
        harness = _sync_harness()
        for failing in ("pending_resolution", "sync_payload"):
            getattr(harness.store, failing).side_effect = sqlite3.Error("db")
            with patch(
                "pomodorough.synchronization_controller.capture_exception",
            ) as capture:
                outcome = harness.controller.sync()
            capture.assert_called_once()
            self.assertTrue(_notices(outcome) or outcome.effects == ())
            getattr(harness.store, failing).side_effect = None
        harness.store.sync_payload.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.synchronization_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.sync()
        capture.assert_not_called()
        self.assertTrue(_notices(outcome))

    def test_apply_sync_sendable_falls_back_unsynced(self) -> None:
        harness = _sync_harness()
        harness.controller.sync_request = {"commands": []}
        harness.store.apply_sync.return_value = []
        harness.store.has_sendable_sync_operations.side_effect = (
            sqlite3.Error("db")
        )
        with patch(
            "pomodorough.synchronization_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.apply_sync({})
        capture.assert_called_once()
        states = [e for e in outcome.effects if isinstance(e, SetAccountState)]
        self.assertTrue(states and states[0].synced is False)

    def test_tick_pending_break_guarded(self) -> None:
        from pomodorough.timer_interaction_controller import (
            TimerInteractionContext,
            TimerInteractionController,
            TimerInteractionPorts,
        )

        store = Mock()
        store.has_pending_auto_break.side_effect = sqlite3.Error("db")
        cloud = SimpleNamespace(authenticated=False, busy=False)
        ports = TimerInteractionPorts(
            context=lambda: TimerInteractionContext(
                store, cloud, False, None, {}, None, [], {}, 0,
                "offline", False,
            ),
            apply_outcome=Mock(), mutation_blocked=Mock(return_value=False),
            issue_command=Mock(), maybe_auto_start_break=Mock(),
            notice=Mock(), task_input_text=Mock(return_value=""),
            clear_task_input=Mock(), task_item_data=Mock(return_value=None),
            invalidate_task_selector=Mock(), render_task_selector=Mock(),
            refresh_duration_spins=Mock(), refresh_auto_breaks=Mock(),
            stop_sound_timer=Mock(), stop_completion_sound=Mock(),
            set_stop_sound_control=Mock(),
        )
        controller = TimerInteractionController(ports)
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = controller.tick()
        capture.assert_called_once()
        self.assertTrue(_notices(outcome))

    def test_active_resolution_skips_pending_read(self) -> None:
        harness = _sync_harness(resolving=True)
        harness.store.pending_resolution.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.synchronization_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.sync()
        capture.assert_not_called()
        harness.store.pending_resolution.assert_not_called()
        harness.ports.continue_history_resolution.assert_called_once()
        self.assertEqual(outcome.effects, ())

    def test_render_network_caches_on_infra_failure(self) -> None:
        from pomodorough.ui_views import MainWindowViewMixin

        mixin = MainWindowViewMixin.__new__(MainWindowViewMixin)
        mixin.store = Mock()
        mixin.store.iroh_room.return_value = {"room_id": "room-1"}
        first = mixin._cached_iroh_room_value()
        self.assertEqual(first, {"room_id": "room-1"})
        mixin.store.iroh_room.side_effect = sqlite3.Error("db")
        with patch(
            "pomodorough.ui_views.capture_exception",
        ) as capture:
            second = mixin._cached_iroh_room_value()
        capture.assert_called_once()
        self.assertEqual(second, {"room_id": "room-1"})


class D51SessionScrubTests(unittest.TestCase):
    PARAMS = (
        "session", "session_id", "sid", "ssid", "room", "endpoint",
        "device", "peer", "ticket", "invite",
    )

    def test_url_params_are_stripped(self) -> None:
        for name in self.PARAMS:
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
        for name in self.PARAMS:
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


class D52SilenceCommentTests(unittest.TestCase):
    def test_validation_comments_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "pomodorough"
        terminal = (root / "terminal.py").read_text(encoding="utf-8")
        storage = (root / "storage.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            terminal.count("# validation stays silent"), 2,
        )
        self.assertGreaterEqual(
            storage.count("# validation stays silent"), 3,
        )

    def test_terminal_validation_returns_silently(self) -> None:
        from pomodorough.terminal import LocalTimer

        timer = LocalTimer.__new__(LocalTimer)
        timer.store = Mock()
        timer.store.task_retargets.side_effect = ValueError("corrupt")
        with patch(
            "pomodorough.terminal.capture_exception",
        ) as capture:
            self.assertEqual(timer._load_retargets(), {})
        capture.assert_not_called()

    def test_storage_history_validation_falls_back(self) -> None:
        from pomodorough.storage import Store

        store = Store.__new__(Store)
        store.task_retargets = Mock(side_effect=ValueError("corrupt"))
        projection = SimpleNamespace(history=[{"timerId": "t-1"}])
        state = {"pending": []}
        with patch(
            "pomodorough.storage.capture_exception",
        ) as capture:
            history = store.projected_history(projection, state)
        capture.assert_not_called()
        self.assertEqual(history, [{"timerId": "t-1"}])


if __name__ == "__main__":
    unittest.main()
