from __future__ import annotations

import asyncio
import sys
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pomodorough.iroh_network import IrohService
from pomodorough.iroh_protocol import IrohProtocolError


class IrohServiceLifecycleMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = IrohService(Path("unused.sqlite3"), "device-matrix-0001")

    def test_availability_and_platform_mapping_cover_supported_hosts(self) -> None:
        with patch.dict(sys.modules, {"iroh": SimpleNamespace()}):
            self.assertEqual(IrohService.availability(), (True, "Iroh 1.1.0 ready"))

        for platform, expected in (
            ("darwin", "macos"),
            ("linux", "linux"),
            ("linux2", "linux"),
            ("win32", "windows"),
        ):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                self.assertEqual(IrohService._platform_name(), expected)
        with patch.object(sys, "platform", "plan9"), self.assertRaises(
            IrohProtocolError
        ):
            IrohService._platform_name()

    def test_public_operations_submit_tracked_coroutines_and_stop_without_loop(self) -> None:
        submitted: list[tuple[object, bool]] = []

        def capture(coroutine: object, *, tracked: bool = False) -> None:
            submitted.append((coroutine, tracked))
            coroutine.close()  # type: ignore[attr-defined]

        async def completed_operation() -> None:
            return None

        def serialize(coroutine: object) -> object:
            coroutine.close()  # type: ignore[attr-defined]
            return completed_operation()

        self.service._room_id = "room-identifier"
        with (
            patch.object(self.service, "_submit", side_effect=capture),
            patch.object(
                self.service, "_serialized", new=Mock(side_effect=serialize)
            ),
        ):
            self.service.start_room("room-identifier", emit_invite=True)
            self.service.join_room(Mock())
            self.service.resume_join("room-identifier")
            self.service.refresh_invite()
            self.service.sync_now()

        self.assertEqual(len(submitted), 5)
        self.assertTrue(all(tracked for _coroutine, tracked in submitted))

        statuses: list[str] = []
        self.service.status_changed.connect(statuses.append)
        self.service._loop = None
        self.service.stop()
        self.assertEqual(statuses, ["NOT CONNECTED"])

    def test_refresh_without_room_reports_failure_without_starting_worker(self) -> None:
        failures: list[str] = []
        self.service.failure.connect(failures.append)

        self.service.refresh_invite()

        self.assertEqual(failures, ["No Iroh room is active."])
        self.assertIsNone(self.service._thread)

    def test_running_required_boundaries_and_details_fail_closed(self) -> None:
        self.assertFalse(self.service.running)
        for accessor in (
            self.service._required_store,
            self.service._required_endpoint,
            self.service._required_context,
        ):
            with self.subTest(accessor=accessor.__name__), self.assertRaises(
                IrohProtocolError
            ):
                accessor()

        details: list[object] = []
        self.service.details_changed.connect(details.append)
        self.service._emit_details()
        self.assertEqual(details, [{}])

        endpoint = SimpleNamespace(is_closed=lambda: False)
        self.service._endpoint = endpoint
        self.assertTrue(self.service.running)
        self.service._closing = True
        self.assertFalse(self.service.running)

    def test_cancel_operations_cancels_every_tracked_future(self) -> None:
        futures = [Future(), Future()]
        self.service._operations.update(futures)

        self.service._cancel_operations()

        self.assertTrue(all(future.cancelled() for future in futures))
        self.assertEqual(self.service._operations, set())

    def test_error_envelope_bounds_peer_text_and_marks_internal_retryable(self) -> None:
        self.service._room_id = "room-identifier"
        self.service._room_secret = bytes(32)
        self.service._endpoint_ticket = "endpoint-ticket"
        request = {"requestId": "request-identifier"}

        internal = self.service._error(request, "internal", "😀" * 400)
        invalid = self.service._error(request, "invalid_request", "bad")

        self.assertLessEqual(len(internal["message"].encode()), 1024)
        self.assertTrue(internal["retryable"])
        self.assertFalse(invalid["retryable"])

    def test_ensure_loop_reuses_live_worker_and_reports_unavailable_runtime(self) -> None:
        loop = Mock()
        self.service._loop = loop
        self.service._thread = SimpleNamespace(is_alive=lambda: True)
        self.assertIs(self.service._ensure_loop(), loop)

        self.service._loop = None
        self.service._thread = None
        statuses: list[str] = []
        failures: list[str] = []
        self.service.status_changed.connect(statuses.append)
        self.service.failure.connect(failures.append)
        with (
            patch.object(IrohService, "availability", return_value=(False, "missing")),
            self.assertRaisesRegex(IrohProtocolError, "missing"),
        ):
            self.service._ensure_loop()
        self.assertEqual(statuses, ["UNAVAILABLE"])
        self.assertEqual(failures, ["missing"])

    def test_submit_closes_coroutine_when_worker_cannot_start(self) -> None:
        async def operation() -> None:
            return None

        coroutine = operation()
        with patch.object(
            self.service, "_ensure_loop", side_effect=IrohProtocolError("unavailable")
        ):
            self.assertIsNone(self.service._submit(coroutine, tracked=True))
        self.assertIsNone(coroutine.cr_frame)

    def test_shutdown_clears_worker_references_even_when_shutdown_future_fails(self) -> None:
        class FailedResult:
            @staticmethod
            def result(timeout: int) -> None:
                if timeout != 10:
                    raise AssertionError(timeout)
                raise RuntimeError("worker failed")

        loop = Mock()
        thread = Mock()
        self.service._loop = loop
        self.service._thread = thread

        def schedule(coroutine: object, scheduled_loop: object) -> FailedResult:
            self.assertIs(scheduled_loop, loop)
            coroutine.close()  # type: ignore[attr-defined]
            return FailedResult()

        with patch.object(asyncio, "run_coroutine_threadsafe", side_effect=schedule):
            self.service.shutdown()

        loop.call_soon_threadsafe.assert_called_once_with(loop.stop)
        thread.join.assert_called_once_with(timeout=10)
        self.assertIsNone(self.service._loop)
        self.assertIsNone(self.service._thread)


class IrohServiceAsyncMatrixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = IrohService(Path("unused.sqlite3"), "device-matrix-0001")
        self.service._room_id = "room-identifier"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "endpoint-ticket"

    async def test_start_room_reuses_live_route_and_emits_invite_only_on_request(
        self,
    ) -> None:
        endpoint = SimpleNamespace(is_closed=lambda: False)
        store = Mock(iroh_room_secret=Mock(return_value=bytes(range(32))))
        self.service._endpoint = endpoint
        self.service._store = store
        self.service._emit_invite = AsyncMock()

        with patch.dict(sys.modules, {"iroh": SimpleNamespace()}):
            await self.service._start_room("room-identifier", emit_invite=False)
            await self.service._start_room("room-identifier", emit_invite=True)

        self.assertEqual(store.iroh_room_secret.call_count, 2)
        self.service._emit_invite.assert_awaited_once_with()

    async def test_emit_invite_reads_room_metadata_and_publishes_current_route(self) -> None:
        self.service._store = Mock(
            iroh_room=Mock(return_value={"roomName": "Matrix room"})
        )
        invites: list[str] = []
        self.service.invite_ready.connect(invites.append)

        with (
            patch.object(
                self.service,
                "_current_endpoint_ticket",
                return_value="fresh-endpoint-ticket",
            ),
            patch(
                "pomodorough.iroh_network.create_invite", return_value="encoded-invite"
            ) as create,
        ):
            await self.service._emit_invite()

        create.assert_called_once_with(
            bytes(range(32)), "fresh-endpoint-ticket", "Matrix room"
        )
        self.assertEqual(invites, ["encoded-invite"])
        self.assertTrue(self.service._invite_requested)

    async def test_emit_invite_fails_when_room_metadata_disappears(self) -> None:
        self.service._store = Mock(iroh_room=Mock(return_value=None))
        with (
            patch.object(
                self.service,
                "_current_endpoint_ticket",
                return_value="fresh-endpoint-ticket",
            ),
            self.assertRaisesRegex(IrohProtocolError, "metadata is missing"),
        ):
            await self.service._emit_invite()

    async def test_operations_request_returns_records_from_storage_boundary(self) -> None:
        records = [{"domain": "autoStart", "operation": {"id": "operation-1"}}]
        store = Mock(iroh_operations=Mock(return_value=records))
        self.service._store = store
        request = {
            "roomId": "room-identifier",
            "requestId": "request-identifier",
            "kind": "operations",
            "refs": [{"domain": "autoStart", "id": "operation-1"}],
        }
        stream = Mock()
        self.service._read_message = AsyncMock(return_value=request)
        self.service._write_message = AsyncMock()

        await self.service._handle_request(stream)

        store.iroh_operations.assert_called_once_with(
            "room-identifier", request["refs"]
        )
        response = self.service._write_message.await_args.args[0]
        self.assertEqual(response["kind"], "operationsResult")
        self.assertEqual(response["records"], records)


if __name__ == "__main__":
    unittest.main()
