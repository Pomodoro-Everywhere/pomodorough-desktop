from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PySide6.QtCore import Qt

from pomodorough.iroh_network import IrohService
from pomodorough.iroh_protocol import ALPN, ImmutableConflict, IrohProtocolError


class FakeEndpoint:
    def __init__(self, *, online_error: BaseException | None = None) -> None:
        self.online_error = online_error
        self.closed = False
        self.online_calls = 0

    async def online(self) -> None:
        self.online_calls += 1
        if self.online_error is not None:
            raise self.online_error

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed

    def addr(self) -> str:
        return "local-address"

    def id(self) -> SimpleNamespace:
        return SimpleNamespace(fmt_short=lambda: "local")


class FakeTicket:
    def __init__(self, endpoint_id: str, label: str = "peer") -> None:
        self.endpoint_id = endpoint_id
        self.label = label

    def endpoint_addr(self) -> SimpleNamespace:
        identity = Mock()
        identity.__str__ = Mock(return_value=self.endpoint_id)
        identity.fmt_short.return_value = self.label
        return SimpleNamespace(id=lambda: identity)


class IrohWorkerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = IrohService(
            Path(self.temporary.name) / "state.sqlite3", "device-12345678"
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.temporary.cleanup()

    def test_worker_runs_tracked_operations_reports_failure_and_cancels(self) -> None:
        registered_loops: list[asyncio.AbstractEventLoop] = []
        ffi = SimpleNamespace(uniffi_set_event_loop=registered_loops.append)
        native = SimpleNamespace(iroh_ffi=ffi)
        failures: list[str] = []
        failure_reported = threading.Event()
        self.service.failure.connect(
            lambda message: (failures.append(message), failure_reported.set()),
            Qt.ConnectionType.DirectConnection,
        )

        async def worker_state() -> tuple[str, bool, bool]:
            return (
                threading.current_thread().name,
                self.service._store is not None,
                self.service._session_lock is not None,
            )

        async def fail() -> None:
            raise RuntimeError("native worker failed")

        with (
            patch.dict(sys.modules, {"iroh": native}),
            patch.object(self.service, "availability", return_value=(True, "ready")),
        ):
            state = self.service._submit(worker_state(), tracked=True)
            self.assertIsNotNone(state)
            self.assertEqual(state.result(timeout=2), ("pomodorough-iroh", True, True))

            failed = self.service._submit(fail(), tracked=True)
            self.assertIsNotNone(failed)
            with self.assertRaisesRegex(RuntimeError, "native worker failed"):
                failed.result(timeout=2)
            self.assertTrue(failure_reported.wait(timeout=2))

            sleeping = self.service._submit(asyncio.sleep(60), tracked=True)
            self.assertIsNotNone(sleeping)
            self.service._cancel_operations()
            with self.assertRaises(CancelledError):
                sleeping.result(timeout=2)
            self.service.shutdown()

        self.assertEqual(failures, ["native worker failed"])
        self.assertEqual(len(registered_loops), 1)
        self.assertTrue(registered_loops[0].is_closed())
        self.assertIsNone(self.service._thread)
        self.assertIsNone(self.service._loop)
        self.assertFalse(self.service._ready.is_set())

    def test_unavailable_native_runtime_fails_without_leaking_coroutine(self) -> None:
        statuses: list[str] = []
        failures: list[str] = []
        self.service.status_changed.connect(statuses.append)
        self.service.failure.connect(failures.append)
        operation = asyncio.sleep(0)

        with patch.object(
            self.service,
            "availability",
            return_value=(False, "native wheel unavailable"),
        ):
            result = self.service._submit(operation, tracked=True)

        self.assertIsNone(result)
        self.assertIsNone(operation.cr_frame)
        self.assertEqual(statuses, ["UNAVAILABLE"])
        self.assertEqual(failures, ["native wheel unavailable"])
        self.assertEqual(self.service._operations, set())


class IrohNativeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = IrohService(
            Path(self.temporary.name) / "state.sqlite3", "device-12345678"
        )
        self.service._room_id = "room-1"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "local-ticket"
        self.service.key_store = SimpleNamespace(load_or_create=lambda: b"k" * 32)

    def tearDown(self) -> None:
        self.service.shutdown()
        self.temporary.cleanup()

    def test_open_endpoint_handles_stale_owner_timeout_and_cancellation(self) -> None:
        async def scenario() -> None:
            self.service._generation = 4
            endpoint = FakeEndpoint()
            native = self._native_with_endpoint(endpoint)
            opened = await self.service._open_room_endpoint(native, 4)
            self.assertEqual(opened, (endpoint, True))

            timed_out = FakeEndpoint(online_error=TimeoutError())
            opened = await self.service._open_room_endpoint(
                self._native_with_endpoint(timed_out), 4
            )
            self.assertEqual(opened, (timed_out, False))

            stale = FakeEndpoint()
            opened = await self.service._open_room_endpoint(
                self._native_with_endpoint(stale), 3
            )
            self.assertIsNone(opened)
            self.assertTrue(stale.closed)
            self.assertEqual(stale.online_calls, 0)

            cancelled = FakeEndpoint(online_error=asyncio.CancelledError())
            with self.assertRaises(asyncio.CancelledError):
                await self.service._open_room_endpoint(
                    self._native_with_endpoint(cancelled), 4
                )
            self.assertTrue(cancelled.closed)

        asyncio.run(scenario())

    def test_stop_endpoint_cancels_tasks_closes_route_and_clears_state(self) -> None:
        statuses: list[str] = []
        details: list[object] = []
        self.service.status_changed.connect(statuses.append)
        self.service.details_changed.connect(details.append)

        async def scenario() -> None:
            endpoint = FakeEndpoint()
            self.service._endpoint = endpoint
            self.service._relay_ready = True
            self.service._invite_requested = True
            self.service._accept_task = asyncio.create_task(asyncio.sleep(60))
            self.service._periodic_task = asyncio.create_task(asyncio.sleep(60))
            connection_task = asyncio.create_task(asyncio.sleep(60))
            self.service._connection_tasks.add(connection_task)

            await self.service._stop_endpoint()

            self.assertTrue(endpoint.closed)
            self.assertTrue(connection_task.cancelled())
            self.assertIsNone(self.service._endpoint)
            self.assertIsNone(self.service._room_id)
            self.assertIsNone(self.service._room_secret)
            self.assertIsNone(self.service._endpoint_ticket)
            self.assertFalse(self.service._relay_ready)
            self.assertFalse(self.service._invite_requested)
            self.assertEqual(self.service._connection_tasks, set())

        asyncio.run(scenario())
        self.assertEqual(statuses, ["NOT CONNECTED"])
        self.assertEqual(details, [{}])

    def test_incoming_connections_enforce_protocol_and_connection_limits(self) -> None:
        async def scenario() -> None:
            handled = AsyncMock()
            self.service._handle_incoming = handled

            wrong_protocol = self._connection(alpn=b"wrong")
            await self.service._accept_incoming(self._incoming(wrong_protocol), 0)
            wrong_protocol.close.assert_called_once_with(1, b"wrong protocol")

            limited = self._connection()
            self.service._authenticated_connections = (
                self.service.MAX_AUTHENTICATED_CONNECTIONS
            )
            await self.service._accept_incoming(self._incoming(limited), 0)
            limited.close.assert_called_once_with(1, b"connection limit")

            accepted = self._connection()
            self.service._authenticated_connections = 0
            await self.service._accept_incoming(self._incoming(accepted), 0)
            handled.assert_awaited_once_with(accepted, 0)
            self.assertEqual(self.service._authenticated_connections, 0)

            handled.side_effect = RuntimeError("bad hello")
            rejected = self._connection()
            await self.service._accept_incoming(self._incoming(rejected), 0)
            rejected.close.assert_called_once_with(1, b"handshake failed")
            self.assertEqual(self.service._authenticated_connections, 0)

            incoming = SimpleNamespace(
                accept=AsyncMock(side_effect=RuntimeError("handshake failed")),
                ignore=AsyncMock(),
            )
            await self.service._accept_incoming(incoming, 0)
            incoming.ignore.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_peer_responses_fail_closed_on_identity_and_request_mismatch(self) -> None:
        async def scenario() -> None:
            stream = SimpleNamespace(send=lambda: object(), recv=lambda: object())
            connection = SimpleNamespace(open_bi=AsyncMock(return_value=stream))
            self.service._write_message = AsyncMock()
            request = self.service._envelope("request-1", "inventory")

            responses = [
                (
                    self.service._envelope("other-request", "inventoryResult"),
                    "request ID",
                    IrohProtocolError,
                ),
                (
                    {**self.service._envelope("request-1", "inventoryResult"), "roomId": "other"},
                    "different room",
                    IrohProtocolError,
                ),
                (
                    self.service._envelope(
                        "request-1",
                        "error",
                        code="immutable_conflict",
                        message="repair required",
                    ),
                    "repair required",
                    ImmutableConflict,
                ),
                (
                    self.service._envelope(
                        "request-1", "error", code="denied", message="rejected"
                    ),
                    "rejected",
                    IrohProtocolError,
                ),
            ]
            for response, message, error_type in responses:
                with self.subTest(message=message):
                    self.service._read_message = AsyncMock(return_value=response)
                    with self.assertRaisesRegex(error_type, message):
                        await self.service._request(connection, request)

            success = self.service._envelope("request-1", "inventoryResult")
            self.service._read_message = AsyncMock(return_value=success)
            self.assertIs(await self.service._request(connection, request), success)

        asyncio.run(scenario())

    def test_fetched_records_must_exactly_match_advertised_references(self) -> None:
        references = [
            {"domain": "tasks", "id": "task-1"},
            {"domain": "genesis", "id": "genesis"},
        ]
        records = [
            {"domain": "tasks", "operation": {"id": "task-1"}},
            {"domain": "genesis", "genesis": {}},
        ]
        advertised = {("tasks", "task-1"): "a", ("genesis", "genesis"): "b"}
        store = SimpleNamespace(insert_remote_iroh_records=Mock(return_value=True))
        connection = object()

        async def scenario() -> None:
            self.service._request = AsyncMock(
                return_value=self.service._envelope(
                    "response", "operationsResult", records=records[:1]
                )
            )
            with self.assertRaisesRegex(IrohProtocolError, "partial or unrequested"):
                await self.service._fetch_records(
                    connection, store, "room-1", references, advertised
                )

            self.service._request.return_value = self.service._envelope(
                "response", "inventoryResult", records=records
            )
            with self.assertRaisesRegex(IrohProtocolError, "wrong operations"):
                await self.service._fetch_records(
                    connection, store, "room-1", references, advertised
                )

            self.service._request.return_value = self.service._envelope(
                "response", "operationsResult", records=records
            )
            changed = await self.service._fetch_records(
                connection, store, "room-1", references, advertised
            )
            self.assertTrue(changed)
            store.insert_remote_iroh_records.assert_called_once_with(
                "room-1", records, advertised
            )

        asyncio.run(scenario())

    def test_authenticated_requests_are_bounded_to_room_and_allowed_kinds(self) -> None:
        store = SimpleNamespace(
            iroh_inventory=Mock(return_value=([{"id": "item"}], "cursor")),
            iroh_operations=Mock(return_value=[{"domain": "tasks"}]),
        )
        self.service._store = store

        async def scenario() -> None:
            wrong_room = await self._handle_request(
                {"roomId": "other", "requestId": "r1", "kind": "inventory"}
            )
            self.assertEqual(wrong_room["code"], "wrong_room")

            inventory = await self._handle_request(
                {
                    "roomId": "room-1",
                    "requestId": "r2",
                    "kind": "inventory",
                    "after": None,
                    "limit": 10,
                }
            )
            self.assertEqual(inventory["kind"], "inventoryResult")
            self.assertEqual(inventory["next"], "cursor")

            unsupported = await self._handle_request(
                {"roomId": "room-1", "requestId": "r3", "kind": "hello"}
            )
            self.assertEqual(unsupported["code"], "invalid_request")

            store.iroh_operations.side_effect = KeyError("missing")
            missing = await self._handle_request(
                {
                    "roomId": "room-1",
                    "requestId": "r4",
                    "kind": "operations",
                    "refs": [],
                }
            )
            self.assertEqual(missing["code"], "not_found")

        asyncio.run(scenario())

    def test_request_storage_errors_are_sanitized_and_bad_frames_are_reset(self) -> None:
        store = SimpleNamespace(iroh_inventory=Mock())
        self.service._store = store

        async def scenario() -> None:
            request = {
                "roomId": "room-1",
                "requestId": "request",
                "kind": "inventory",
                "after": None,
                "limit": 10,
            }
            store.iroh_inventory.side_effect = ImmutableConflict("conflict details")
            conflict = await self._handle_request(request)
            self.assertEqual(conflict["code"], "immutable_conflict")

            store.iroh_inventory.side_effect = ValueError("invalid limit")
            invalid = await self._handle_request(request)
            self.assertEqual(invalid["code"], "invalid_request")
            self.assertEqual(invalid["message"], "invalid limit")

            recv = SimpleNamespace(stop=AsyncMock())
            send = SimpleNamespace(reset=AsyncMock())
            stream = SimpleNamespace(recv=lambda: recv, send=lambda: send)
            self.service._read_message = AsyncMock(
                side_effect=IrohProtocolError("bad frame")
            )
            await self.service._handle_request(stream)
            recv.stop.assert_awaited_once_with(1)
            send.reset.assert_awaited_once_with(1)

        asyncio.run(scenario())

    def test_peer_sync_checks_saved_and_connected_identity_before_exchange(self) -> None:
        connection = SimpleNamespace(remote_id=lambda: "peer-1")
        endpoint = FakeEndpoint()
        endpoint.connect = AsyncMock(return_value=connection)
        self.service._endpoint = endpoint
        store = SimpleNamespace(
            capture_local_iroh_records=Mock(),
            iroh_peers=Mock(
                return_value=[
                    {"endpointTicket": "ticket", "endpointId": "peer-1"}
                ]
            ),
        )
        self.service._store = store
        self.service._exchange = AsyncMock()
        self.service._emit_ready_status = Mock()
        self.service._emit_details = Mock()
        native = self._native_with_ticket(FakeTicket("peer-1"))

        async def scenario() -> None:
            with patch.dict(sys.modules, {"iroh": native}):
                self.assertTrue(await self.service._sync_known_peers())
                self.service._exchange.assert_awaited_once_with(connection)

                native.EndpointTicket.from_string.return_value = FakeTicket("changed")
                self.service._exchange.reset_mock()
                self.assertFalse(await self.service._sync_known_peers())
                self.service._exchange.assert_not_awaited()

                native.EndpointTicket.from_string.return_value = FakeTicket("peer-1")
                connection.remote_id = lambda: "impostor"
                self.assertFalse(await self.service._sync_known_peers())
                self.service._exchange.assert_not_awaited()

        asyncio.run(scenario())

    def test_sync_conflict_stops_route_and_requires_repair(self) -> None:
        store = SimpleNamespace(
            capture_local_iroh_records=Mock(
                side_effect=ImmutableConflict("immutable collision")
            )
        )
        self.service._store = store
        self.service._endpoint = FakeEndpoint()
        self.service._stop_endpoint = AsyncMock()
        self.service._emit_details = Mock()
        statuses: list[str] = []
        self.service.status_changed.connect(statuses.append)

        self.assertFalse(asyncio.run(self.service._sync_known_peers()))
        self.service._stop_endpoint.assert_awaited_once_with()
        self.service._emit_details.assert_called_once_with()
        self.assertEqual(statuses, ["REPAIR REQUIRED"])

    def test_route_refresh_reissues_invite_only_after_current_route_is_online(self) -> None:
        async def scenario() -> None:
            endpoint = FakeEndpoint()
            self.service._endpoint = endpoint
            self.service._generation = 8
            self.service._invite_requested = True
            self.service._current_endpoint_ticket = Mock(return_value="new-ticket")
            self.service._emit_ready_status = Mock()
            self.service._emit_invite = AsyncMock()

            await self.service._refresh_online_route(7)
            self.assertFalse(self.service._relay_ready)
            self.service._current_endpoint_ticket.assert_not_called()

            await self.service._refresh_online_route(8)
            self.assertTrue(self.service._relay_ready)
            self.service._current_endpoint_ticket.assert_called_once_with()
            self.service._emit_ready_status.assert_called_once_with()
            self.service._emit_invite.assert_awaited_once_with()

            endpoint.online_error = TimeoutError()
            self.service._relay_ready = False
            await self.service._refresh_online_route(8)
            self.assertFalse(self.service._relay_ready)

        asyncio.run(scenario())

    @staticmethod
    def _native_with_endpoint(endpoint: FakeEndpoint) -> SimpleNamespace:
        endpoint_type = SimpleNamespace(bind=AsyncMock(return_value=endpoint))
        return SimpleNamespace(
            Endpoint=endpoint_type,
            EndpointOptions=lambda **values: SimpleNamespace(**values),
            preset_n0=lambda: "preset",
        )

    @staticmethod
    def _native_with_ticket(ticket: FakeTicket) -> SimpleNamespace:
        endpoint_ticket = SimpleNamespace(from_string=Mock(return_value=ticket))
        return SimpleNamespace(EndpointTicket=endpoint_ticket)

    async def _handle_request(self, request: dict[str, object]) -> dict[str, object]:
        stream = SimpleNamespace(recv=lambda: object(), send=lambda: object())
        self.service._read_message = AsyncMock(return_value=request)
        self.service._write_message = AsyncMock()
        await self.service._handle_request(stream)
        return self.service._write_message.await_args.args[0]

    @staticmethod
    def _connection(*, alpn: bytes = ALPN) -> SimpleNamespace:
        return SimpleNamespace(
            alpn=lambda: alpn,
            close=Mock(),
        )

    @staticmethod
    def _incoming(connection: SimpleNamespace) -> SimpleNamespace:
        accepted = SimpleNamespace(connect=AsyncMock(return_value=connection))
        return SimpleNamespace(accept=AsyncMock(return_value=accepted), ignore=AsyncMock())


if __name__ == "__main__":
    unittest.main()
