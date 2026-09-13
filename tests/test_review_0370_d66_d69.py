"""0.37.0 review D66-D69: controller infra boundary, JSON scrub, mapping, close."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.shared_core import (
    SharedCoreABIError,
    SharedCoreError,
    SharedCoreLoadError,
    SharedCoreOperationError,
)


class D66ControllerBoundaryTests(unittest.TestCase):
    def test_modules_import_shared_core_error(self) -> None:
        import pomodorough.account_resolution_controller as arc
        import pomodorough.replication_controller as rpc
        import pomodorough.synchronization_controller as syc
        import pomodorough.timer_interaction_controller as tic
        import pomodorough.ui_controller as uic

        for module in (arc, rpc, syc, tic, uic):
            self.assertIs(module.SharedCoreError, SharedCoreError)

    def test_ui_controller_guards_all_four_sites(self) -> None:
        source = Path("src/pomodorough/ui_controller.py").read_text()
        if not Path("src/pomodorough/ui_controller.py").exists():
            source = Path("desktop/src/pomodorough/ui_controller.py").read_text()
        needle = "except (OSError, sqlite3.Error, SharedCoreError)"
        self.assertEqual(source.count(needle), 4)

    def test_account_resolution_captures_shared_core_error(self) -> None:
        from pomodorough.account_resolution_controller import (
            AccountResolutionContext,
            AccountResolutionController,
        )

        error = SharedCoreError("wasm load failed")
        store = Mock()
        store.pending_resolution.side_effect = error
        context = AccountResolutionContext(store, Mock(), Mock(), None)
        controller = AccountResolutionController(Mock(context=Mock(return_value=context)))
        with patch(
            "pomodorough.account_resolution_controller.capture_exception"
        ) as capture:
            try:
                outcome = controller.activate_persisted_resolution()
            except SharedCoreError:
                self.fail("SharedCoreError escaped account controller")
        capture.assert_called_once_with(error)
        self.assertIn(str(error), str(outcome.effects))

    def test_replication_captures_shared_core_error(self) -> None:
        from pomodorough.replication_controller import (
            ReplicationContext,
            ReplicationController,
        )

        error = SharedCoreError("wasm load failed")
        store = Mock()
        store.set_replication_mode.side_effect = error
        strings = Mock()
        strings.text.side_effect = lambda key: key
        context = ReplicationContext(store, Mock(), None, strings, False)
        ports = Mock()
        ports.context.return_value = context
        controller = ReplicationController(ports)
        with patch(
            "pomodorough.replication_controller.capture_exception"
        ) as capture:
            try:
                outcome = controller.replication_mode_changed("centralized")
            except SharedCoreError:
                self.fail("SharedCoreError escaped replication controller")
        capture.assert_called_once_with(error)
        self.assertIn("wasm load failed", str(outcome.effects))

    def test_synchronization_captures_shared_core_error(self) -> None:
        from pomodorough.synchronization_controller import (
            SynchronizationContext,
            SynchronizationController,
        )

        error = SharedCoreError("wasm load failed")
        store = Mock()
        store.capture_local_iroh_records.side_effect = error
        context = SynchronizationContext(
            store, Mock(), Mock(), Mock(), False, 0, "iroh", False, False
        )
        ports = Mock()
        ports.context.return_value = context
        controller = SynchronizationController(ports)
        with patch(
            "pomodorough.synchronization_controller.capture_exception"
        ) as capture:
            try:
                controller.sync()
            except SharedCoreError:
                self.fail("SharedCoreError escaped synchronization controller")
        capture.assert_called_once_with(error)
        ports.iroh_failure.assert_called_once_with(str(error))

    def test_timer_tick_captures_shared_core_error(self) -> None:
        from pomodorough.timer_interaction_controller import (
            TimerInteractionContext,
            TimerInteractionController,
        )

        error = SharedCoreError("wasm load failed")
        store = Mock()
        store.has_pending_auto_break.side_effect = error
        cloud = Mock()
        cloud.authenticated = False
        cloud.busy = False
        context = TimerInteractionContext(
            store, cloud, False, None, {}, None, [], {}, 0, "offline", False
        )
        ports = Mock()
        ports.context.return_value = context
        controller = TimerInteractionController(ports)
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception"
        ) as capture:
            try:
                outcome = controller.tick()
            except SharedCoreError:
                self.fail("SharedCoreError escaped timer controller")
        capture.assert_called_once_with(error)
        self.assertIn("wasm load failed", str(outcome.effects))

    def test_read_resolution_corruption_captures_shared_core_error(self) -> None:
        from types import SimpleNamespace

        from pomodorough.ui_controller import ApplicationController

        error = SharedCoreError("wasm load failed")
        store = Mock()
        store.pending_resolution.side_effect = error
        account = SimpleNamespace(resolution_corruption=None)
        controller = SimpleNamespace(store=store, account_resolution=account)
        with patch("pomodorough.ui_controller.capture_exception") as capture:
            try:
                ApplicationController._read_resolution_corruption(controller)
            except SharedCoreError:
                self.fail("SharedCoreError escaped _read_resolution_corruption")
        capture.assert_called_once_with(error)
        self.assertEqual(account.resolution_corruption, str(error))

    def test_read_resolution_corruption_validation_stays_silent(self) -> None:
        from types import SimpleNamespace

        from pomodorough.ui_controller import ApplicationController

        store = Mock()
        store.pending_resolution.side_effect = ValueError("corrupted")
        account = SimpleNamespace(resolution_corruption=None)
        controller = SimpleNamespace(store=store, account_resolution=account)
        with patch("pomodorough.ui_controller.capture_exception") as capture:
            ApplicationController._read_resolution_corruption(controller)
        capture.assert_not_called()
        self.assertEqual(account.resolution_corruption, "corrupted")


class D67UnquotedJsonScrubTests(unittest.TestCase):
    def test_unquoted_scalars_are_filtered(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        payloads = ('{"token":12345}', '{"session":null}', '{"room_id": 987}')
        for payload in payloads:
            with self.subTest(payload=payload):
                rendered = json.dumps(scrub_sentry_event({"message": payload}, None))
                self.assertIn("[Filtered]", rendered)
        self.assertNotIn("12345", json.dumps(
            scrub_sentry_event({"message": '{"token":12345}'}, None)))
        self.assertNotIn("987", json.dumps(
            scrub_sentry_event({"message": '{"room_id": 987}'}, None)))
        rendered = json.dumps(
            scrub_sentry_event({"message": '{"session":null}'}, None))
        self.assertNotIn("null}", rendered.replace("[Filtered]", ""))

    def test_quoted_branch_still_preserves_quotes(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        rendered = json.dumps(
            scrub_sentry_event({"message": '{"token":"abc123"}'}, None))
        self.assertNotIn("abc123", rendered)
        message = json.loads(rendered)["message"]
        self.assertIn('"[Filtered]"', message)

    def test_benign_unquoted_values_stay(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        rendered = json.dumps(
            scrub_sentry_event({"message": '{"next":123}'}, None))
        self.assertIn("123", rendered)
        self.assertNotIn("[Filtered]", rendered)


class D68OperationMappingTests(unittest.TestCase):
    @staticmethod
    def _tick_reservation(exc: BaseException) -> object:
        from pomodorough.storage_generation import GenerationReservation

        class FailingCore:
            def dispatch(self, _op: str, _inp: object) -> object:
                raise exc

        return GenerationReservation(
            lambda k, d: {"wallMs": 0, "counter": 0} if k == "hlc" else 0,
            lambda ms, **_: ms,
            lambda: FailingCore(),
        )

    def test_mapping_sites_narrow_to_operation_error(self) -> None:
        roots = (Path("src/pomodorough"), Path("desktop/src/pomodorough"))
        root = next(p for p in roots if p.exists())
        files = (
            "storage.py",
            "storage_sync.py",
            "storage_generation.py",
            "storage_completion.py",
            "storage_canonical_reconciliation.py",
            "storage_replication_projection.py",
        )
        for name in files:
            with self.subTest(file=name):
                source = (root / name).read_text()
                self.assertIn("except SharedCoreOperationError", source)
                self.assertNotIn("except SharedCoreError", source)

    def test_tick_operation_error_becomes_value_error(self) -> None:
        operation = SharedCoreOperationError("hlc.tick.v1", "rejected")
        reservation = self._tick_reservation(operation)
        with self.assertRaises(ValueError):
            reservation.reserve(1_000)

    def test_tick_abi_error_propagates_as_infra(self) -> None:
        failure = SharedCoreABIError("dispatch returned an empty result buffer")
        reservation = self._tick_reservation(failure)
        with self.assertRaises(SharedCoreABIError):
            reservation.reserve(1_000)

    def test_tick_load_error_propagates_as_infra(self) -> None:
        failure = SharedCoreLoadError("failed to instantiate shared core")
        reservation = self._tick_reservation(failure)
        with self.assertRaises(SharedCoreLoadError):
            reservation.reserve(1_000)

    def test_completion_policy_splits_operation_from_abi(self) -> None:
        from pomodorough.storage_completion import TimerCompletionPolicy

        operation = SharedCoreOperationError("timer.completionPlan.v1", "nope")
        abi = SharedCoreABIError("dispatch returned an empty result buffer")

        class FailingCore:
            def __init__(self, exc: BaseException) -> None:
                self.exc = exc

            def dispatch(self, _op: str, _inp: object) -> object:
                raise self.exc

        policy = TimerCompletionPolicy(lambda: FailingCore(operation),
                                       lambda: "d1", lambda: "offline",
                                       lambda _k, _d: None)
        with self.assertRaises(ValueError):
            policy._plan({})
        policy = TimerCompletionPolicy(lambda: FailingCore(abi),
                                       lambda: "d1", lambda: "offline",
                                       lambda _k, _d: None)
        with self.assertRaises(SharedCoreABIError):
            policy._plan({})


class D69AppCloseGuardTests(unittest.TestCase):
    def test_close_store_captures_sqlite_error(self) -> None:
        from pomodorough import app as app_module

        error = sqlite3.OperationalError("WAL checkpoint failed")
        store = Mock()
        store.close.side_effect = error
        with patch("pomodorough.app.capture_exception") as capture:
            try:
                app_module._close_store(store)
            except Exception as exc:  # noqa: BLE001 - guard must not raise
                self.fail(f"close failure escaped guarded slot: {exc!r}")
        capture.assert_called_once_with(error)

    def test_close_store_captures_shared_core_error(self) -> None:
        from pomodorough import app as app_module

        error = SharedCoreError("wasm teardown failed")
        store = Mock()
        store.close.side_effect = error
        with patch("pomodorough.app.capture_exception") as capture:
            try:
                app_module._close_store(store)
            except Exception as exc:  # noqa: BLE001 - guard must not raise
                self.fail(f"close failure escaped guarded slot: {exc!r}")
        capture.assert_called_once_with(error)

    def test_main_guards_quit_slot(self) -> None:
        from pomodorough import app as app_module

        store = Mock()
        store.device_id = "device-1"
        store.close.side_effect = sqlite3.OperationalError("WAL checkpoint failed")
        app_mock = Mock()
        app_mock.exec.return_value = 0
        lock = Mock()
        lock.tryLock.return_value = True
        with (
            patch.object(app_module, "init_sentry_from_environment",
                         return_value=False),
            patch.object(app_module, "QApplication", return_value=app_mock),
            patch.object(app_module, "QIcon"),
            patch.object(app_module, "_instance_lock", return_value=lock),
            patch.object(app_module, "Store", return_value=store),
            patch.object(app_module, "CloudService", return_value=Mock()),
            patch.object(app_module, "_iroh_service", return_value=Mock()),
            patch.object(app_module, "MainWindow", return_value=Mock()),
            patch("pomodorough.app.capture_exception") as capture,
        ):
            try:
                result = app_module.main()
            except Exception as exc:  # noqa: BLE001 - quit path must not raise
                self.fail(f"aboutToQuit path escaped app.main: {exc!r}")
            self.assertEqual(result, 0)
            slots = [c.args[0] for c in app_mock.aboutToQuit.connect.call_args_list]
            self.assertNotIn(store.close, slots)
            for slot in slots:
                try:
                    slot()
                except Exception as exc:  # noqa: BLE001 - slots must not raise
                    self.fail(f"aboutToQuit slot raised: {exc!r}")
            capture.assert_called_once_with(store.close.side_effect)


if __name__ == "__main__":
    unittest.main()
