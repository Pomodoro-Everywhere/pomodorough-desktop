from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.ui_controller import WindowApplicationController


class _Signal:
    def __init__(self, name: str, connections: list[str]) -> None:
        self.name = name
        self.connections = connections

    def connect(self, _callback: object) -> None:
        self.connections.append(self.name)


class ApplicationControllerLifecycleTests(unittest.TestCase):
    def test_application_owns_one_controller_for_each_responsibility(self) -> None:
        controller = WindowApplicationController()
        components = {
            "timer": controller.timer_interactions,
            "account": controller.account_resolution,
            "synchronization": controller.synchronization,
            "replication": controller.replication,
        }

        for name, component in components.items():
            self.assertIs(component, controller.__dict__[f"_{name}_controller"])
            self.assertIs(component._ports.context.__self__, controller)

        owned_state = {
            "notified_timer_id",
            "account_synced",
            "sync_request",
            "mode",
        }
        self.assertTrue(owned_state.isdisjoint(controller.__dict__))

    def test_cloud_callbacks_connect_in_lifecycle_order(self) -> None:
        connections: list[str] = []
        names = (
            "signed_in",
            "signed_out",
            "session_expired",
            "sync_ready",
            "bootstrap_ready",
            "bootstrap_resolved",
            "bootstrap_conflict",
            "revision_available",
            "authorization_stale",
            "failure",
            "account_deleted",
            "account_deletion_failed",
        )
        controller = WindowApplicationController()
        controller.cloud = SimpleNamespace(
            **{name: _Signal(name, connections) for name in names}
        )

        controller._connect_cloud()

        self.assertEqual(connections, list(names))

    def test_finished_timer_command_renders_before_sync_and_window_restore(
        self,
    ) -> None:
        events: list[object] = []

        class Controller(WindowApplicationController):
            timer: ClassVar[dict[str, str]] = {"phase": "focus"}
            store = None
            cloud = None
            settings: ClassVar[dict[str, object]] = {
                "selectedPhase": "focus",
                "durationsMs": {"focus": 1},
            }
            user = None
            tasks: ClassVar[list[dict[str, object]]] = []
            known_tasks: ClassVar[dict[str, dict[str, object]]] = {}
            _closed = False
            _projection_now_ms = 0

            def _load_state(self) -> None:
                events.append("load")

            def _render(self) -> None:
                events.append("render")

            def _sync(self) -> None:
                events.append("sync")

            def _show_window(self) -> None:
                events.append("show")

        controller = Controller()
        controller.timer_interactions.auto_finish_in_progress = True
        with (
            patch("pomodorough.ui_controller.time.monotonic", return_value=10.0),
            patch(
                "pomodorough.ui_controller.QTimer.singleShot",
                side_effect=lambda delay, _callback: events.append(
                    ("single_shot", delay)
                ),
            ),
        ):
            controller._after_timer_command("finish", automatic=True)

        self.assertEqual(
            events,
            ["load", "render", "sync", ("single_shot", 1200), "show"],
        )
        self.assertFalse(controller._auto_finish_in_progress)
        self.assertEqual(controller._auto_break_not_before, 11.2)

    def test_sync_retry_has_one_waiter_and_retries_only_after_idle(self) -> None:
        scheduled: list[tuple[int, object]] = []
        syncs: list[str] = []

        class Controller(WindowApplicationController):
            store = None
            cloud = SimpleNamespace(busy=True)
            iroh = None
            strings = None
            _closed = False
            revision = 0

            def _sync(self) -> None:
                syncs.append("sync")

        controller = Controller()
        controller.synchronization.sync_waiting = False
        with patch(
            "pomodorough.ui_controller.QTimer.singleShot",
            side_effect=lambda delay, callback: scheduled.append((delay, callback)),
        ):
            controller._sync_when_available()
            controller._sync_when_available()
            self.assertEqual([delay for delay, _ in scheduled], [100])

            controller._retry_sync()
            self.assertEqual([delay for delay, _ in scheduled], [100, 100])
            self.assertTrue(controller._sync_waiting)

            controller.cloud.busy = False
            controller._retry_sync()

        self.assertFalse(controller._sync_waiting)
        self.assertEqual(syncs, ["sync"])

    def test_resolution_conflict_resets_request_before_preview_retry(self) -> None:
        events: list[tuple[object, ...]] = []

        class Store:
            def discard_pending_resolution(self, user_id: str, request_id: str) -> bool:
                events.append(("discard", user_id, request_id))
                return True

        class StatusBar:
            def showMessage(self, message: str, duration: int) -> None:
                events.append(("status", message, duration))

        class Strings:
            @staticmethod
            def text(key: str) -> str:
                return {"resolution.refreshing": " Refreshing"}.get(key, key)

        class Controller(WindowApplicationController):
            store = Store()
            cloud = None
            strings = Strings()
            user = None

            def statusBar(self) -> StatusBar:
                return StatusBar()

            def _continue_history_resolution(self) -> None:
                events.append(
                    (
                        "continue",
                        self._resolution_phase,
                        self._resolution_request_id,
                        self._resolution_retry_paused,
                    )
                )

        controller = Controller()
        controller.account_resolution.history_resolution_active = True
        controller.account_resolution.resolution_user = {"id": "user-1"}
        controller.account_resolution.resolution_request_id = "request-1"
        controller.account_resolution.resolution_phase = "resolve"
        controller.account_resolution.resolution_preview = {"revision": 4}
        controller.account_resolution.resolution_retry_paused = True
        controller._bootstrap_conflict({"message": "Remote changed."})

        self.assertEqual(
            events,
            [
                ("discard", "user-1", "request-1"),
                ("status", "Remote changed. Refreshing", 10_000),
                ("continue", "preview", None, False),
            ],
        )
        self.assertIsNone(controller._resolution_preview)

    def test_iroh_projection_refreshes_state_before_render(self) -> None:
        events: list[str] = []

        class Controller(WindowApplicationController):
            def _load_state(self) -> None:
                events.append("load")

            def _render(self) -> None:
                events.append("render")

        controller = Controller()
        controller.replication.mode = "iroh"
        controller._iroh_projection_changed()

        self.assertEqual(events, ["load", "render"])

    def test_loaded_timer_change_stops_previous_alert_first(self) -> None:
        events: list[str] = []

        class Controller(WindowApplicationController):
            store = None
            cloud = None
            timer: ClassVar[dict[str, str]] = {
                "id": "new",
                "phase": "short_break",
                "status": "running",
            }
            settings: ClassVar[dict[str, object]] = {
                "selectedPhase": "short_break",
                "durationsMs": {"short_break": 1},
            }
            user = None
            tasks: ClassVar[list[dict[str, object]]] = []
            known_tasks: ClassVar[dict[str, dict[str, object]]] = {}
            _closed = False
            _projection_now_ms = 0

            def _stop_sound(self) -> None:
                events.append("stop_sound")
                self._sound_active = False

        controller = Controller()
        controller.timer_interactions.sound_active = True
        controller.timer_interactions.alert_timer_identity = (
            "old",
            "focus",
            "completed",
        )
        controller._reconcile_loaded_timer(None, {"new"})

        self.assertEqual(events, ["stop_sound"])
        self.assertEqual(controller.provisional_auto_break_timer_ids, set())


if __name__ == "__main__":
    unittest.main()
