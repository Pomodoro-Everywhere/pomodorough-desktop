from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pomodorough.iroh_network import IrohService
from pomodorough.iroh_protocol import ALPN, ImmutableConflict, IrohProtocolError


class Address:
    def __init__(self, identity: str = "peer-identity") -> None:
        self.identity = identity

    def id(self) -> object:
        return SimpleNamespace(
            __str__=lambda _self: self.identity,
            fmt_short=lambda: self.identity[:4],
        )


class Ticket:
    def __init__(self, identity: str = "peer-identity") -> None:
        self.address = Address(identity)

    def endpoint_addr(self) -> Address:
        return self.address


class AsyncServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = IrohService(Path("unused.sqlite3"), "device-matrix-0001")
        self.service._room_id = "room-identifier"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "endpoint-ticket"
        self.store = Mock()
        self.service._store = self.store

    async def test_open_endpoint_closes_stale_routes_before_and_after_online(self) -> None:
        endpoint = SimpleNamespace(close=AsyncMock(), online=AsyncMock())
        iroh = SimpleNamespace(
            Endpoint=SimpleNamespace(bind=AsyncMock(return_value=endpoint)),
            EndpointOptions=Mock(return_value="options"),
            preset_n0=Mock(return_value="preset"),
        )
        self.service.key_store = Mock(load_or_create=Mock(return_value=b"k" * 32))
        self.service._generation = 2
        self.service._closing = True
        self.assertIsNone(await self.service._open_room_endpoint(iroh, 2))
        endpoint.close.assert_awaited_once()

        self.service._closing = False
        endpoint.close.reset_mock()
        endpoint.online.side_effect = lambda: setattr(self.service, "_generation", 3)
        self.assertIsNone(await self.service._open_room_endpoint(iroh, 2))
        endpoint.close.assert_awaited_once()

    async def test_open_endpoint_classifies_online_timeout_and_cancellation(self) -> None:
        endpoint = SimpleNamespace(close=AsyncMock(), online=AsyncMock(side_effect=TimeoutError))
        iroh = SimpleNamespace(
            Endpoint=SimpleNamespace(bind=AsyncMock(return_value=endpoint)),
            EndpointOptions=Mock(),
            preset_n0=Mock(),
        )
        self.service.key_store = Mock(load_or_create=Mock(return_value=b"k" * 32))
        self.service._generation = 1
        opened = await self.service._open_room_endpoint(iroh, 1)
        self.assertEqual(opened, (endpoint, False))
        endpoint.online.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.service._open_room_endpoint(iroh, 1)
        endpoint.close.assert_awaited_once()

    async def test_stop_endpoint_cancels_tasks_and_closes_live_endpoint(self) -> None:
        endpoint = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
        task = asyncio.create_task(asyncio.sleep(30))
        connection_task = asyncio.create_task(asyncio.sleep(30))
        self.service._endpoint = endpoint
        self.service._accept_task = task
        self.service._connection_tasks.add(connection_task)
        statuses: list[str] = []
        details: list[object] = []
        self.service.status_changed.connect(statuses.append)
        self.service.details_changed.connect(details.append)
        await self.service._stop_endpoint()
        self.assertTrue(task.cancelled())
        self.assertTrue(connection_task.cancelled())
        endpoint.close.assert_awaited_once()
        self.assertEqual((statuses[-1], details[-1]), ("NOT CONNECTED", {}))

    async def test_accept_loop_retries_errors_rejects_overflow_and_stops_at_none(self) -> None:
        incoming = SimpleNamespace(ignore=AsyncMock(side_effect=OSError("ignore failed")))
        endpoint = SimpleNamespace(
            is_closed=lambda: False,
            accept_next=AsyncMock(side_effect=[OSError("retry"), incoming, None]),
        )
        self.service._endpoint = endpoint
        self.service._generation = 4
        self.service._connection_tasks = {Mock() for _ in range(self.service.MAX_PENDING_HANDSHAKES)}
        with patch("pomodorough.iroh_network.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.service._accept_loop(4)
        sleep.assert_awaited_once_with(0.1)
        incoming.ignore.assert_awaited_once()

    async def test_accept_incoming_rejects_protocol_limit_and_handshake_failures(self) -> None:
        connection = SimpleNamespace(alpn=lambda: b"wrong", close=Mock())
        incoming = SimpleNamespace(
            accept=AsyncMock(return_value=SimpleNamespace(connect=AsyncMock(return_value=connection))),
            ignore=AsyncMock(),
        )
        await self.service._accept_incoming(incoming, self.service._generation)
        connection.close.assert_called_with(1, b"wrong protocol")

        connection.alpn = lambda: ALPN
        connection.close.reset_mock()
        self.service._authenticated_connections = self.service.MAX_AUTHENTICATED_CONNECTIONS
        await self.service._accept_incoming(incoming, self.service._generation)
        connection.close.assert_called_with(1, b"connection limit")

        incoming.accept.side_effect = OSError("handshake")
        await self.service._accept_incoming(incoming, self.service._generation)
        incoming.ignore.assert_awaited_once()

    async def test_handle_request_covers_rejection_and_every_response_family(self) -> None:
        stream = SimpleNamespace(
            recv=Mock(return_value=SimpleNamespace(stop=AsyncMock())),
            send=Mock(return_value=SimpleNamespace(reset=AsyncMock())),
        )
        self.service._read_message = AsyncMock(side_effect=IrohProtocolError("bad frame"))
        await self.service._handle_request(stream)
        stream.recv().stop.assert_awaited_once_with(1)
        stream.send().reset.assert_awaited_once_with(1)

        self.service._write_message = AsyncMock()
        cases = [
            ({"roomId": "other", "requestId": "r", "kind": "inventory"}, "wrong_room"),
            ({"roomId": "room-identifier", "requestId": "r", "kind": "other"}, "invalid_request"),
        ]
        for request, code in cases:
            self.service._read_message = AsyncMock(return_value=request)
            await self.service._handle_request(stream)
            self.assertEqual(self.service._write_message.await_args.args[0]["code"], code)

    async def test_handle_request_maps_storage_success_not_found_conflict_and_value_error(self) -> None:
        stream = SimpleNamespace(recv=Mock(), send=Mock())
        self.service._write_message = AsyncMock()
        inventory = {"roomId": "room-identifier", "requestId": "r", "kind": "inventory", "after": None, "limit": 2}
        self.service._read_message = AsyncMock(return_value=inventory)
        self.store.iroh_inventory.return_value = ([{"id": "x"}], "next")
        await self.service._handle_request(stream)
        self.assertEqual(self.service._write_message.await_args.args[0]["kind"], "inventoryResult")

        operations = {"roomId": "room-identifier", "requestId": "r", "kind": "operations", "refs": []}
        self.service._read_message.return_value = operations
        for failure, code in ((KeyError("missing"), "not_found"), (ImmutableConflict("conflict"), "immutable_conflict"), (ValueError("invalid"), "invalid_request")):
            self.store.iroh_operations.side_effect = failure
            await self.service._handle_request(stream)
            self.assertEqual(self.service._write_message.await_args.args[0]["code"], code)

    async def test_sync_known_peers_handles_absence_conflict_empty_and_failed_peer(self) -> None:
        self.service._endpoint = None
        self.assertFalse(await self.service._sync_known_peers())
        self.service._endpoint = SimpleNamespace(connect=AsyncMock())
        self.store.capture_local_iroh_records.side_effect = ImmutableConflict("conflict")
        self.service._stop_endpoint = AsyncMock()
        self.service._emit_details = Mock()
        self.assertFalse(await self.service._sync_known_peers())
        self.service._stop_endpoint.assert_awaited_once()

        self.store.capture_local_iroh_records.side_effect = None
        self.store.iroh_peers.return_value = []
        self.assertTrue(await self.service._sync_known_peers())
        self.store.iroh_peers.return_value = [{"endpointTicket": "ticket", "endpointId": "expected"}]
        fake_iroh = SimpleNamespace(EndpointTicket=SimpleNamespace(from_string=Mock(side_effect=ValueError("bad"))))
        with patch.dict(sys.modules, {"iroh": fake_iroh}):
            self.assertFalse(await self.service._sync_known_peers())

    async def test_fetch_records_rejects_wrong_partial_and_accepts_exact_sets(self) -> None:
        references = [{"domain": "genesis", "id": "genesis"}]
        self.service._request = AsyncMock(return_value={"kind": "inventoryResult"})
        with self.assertRaisesRegex(IrohProtocolError, "wrong operations"):
            await self.service._fetch_records(Mock(), self.store, "room", references, {})
        self.service._request.return_value = {"kind": "operationsResult", "records": []}
        with self.assertRaisesRegex(IrohProtocolError, "partial"):
            await self.service._fetch_records(Mock(), self.store, "room", references, {})
        record = {"domain": "genesis", "operation": {}}
        self.service._request.return_value = {"kind": "operationsResult", "records": [record]}
        self.store.insert_remote_iroh_records.return_value = True
        self.assertTrue(await self.service._fetch_records(Mock(), self.store, "room", references, {}))

    async def test_request_rejects_identity_room_and_peer_errors(self) -> None:
        message = {"kind": "inventory", "requestId": "request", "roomId": "room-identifier"}
        self.service._write_message = AsyncMock()
        self.service._read_message = AsyncMock()
        connection = SimpleNamespace(open_bi=AsyncMock(return_value=SimpleNamespace(send=Mock(), recv=Mock())))
        for response, error in (
            ({"requestId": "other", "roomId": "room-identifier", "kind": "inventoryResult"}, "request ID"),
            ({"requestId": "request", "roomId": "other", "kind": "inventoryResult"}, "different room"),
            ({"requestId": "request", "roomId": "room-identifier", "kind": "error", "code": "invalid_request", "message": "peer rejected"}, "peer rejected"),
        ):
            self.service._read_message.return_value = response
            with self.assertRaisesRegex(IrohProtocolError, error):
                await self.service._request(connection, message)
        self.service._read_message.return_value = {"requestId": "request", "roomId": "room-identifier", "kind": "error", "code": "immutable_conflict", "message": "repair"}
        with self.assertRaises(ImmutableConflict):
            await self.service._request(connection, message)

    async def test_refresh_route_covers_ready_timeout_stale_and_invite_paths(self) -> None:
        endpoint = SimpleNamespace(online=AsyncMock(side_effect=TimeoutError))
        self.service._endpoint = endpoint
        self.service._relay_ready = True
        await self.service._refresh_online_route(self.service._generation)
        endpoint.online.assert_not_awaited()
        self.service._relay_ready = False
        await self.service._refresh_online_route(self.service._generation)
        endpoint.online.assert_awaited_once()

        endpoint.online.side_effect = None
        self.service._generation = 7
        self.service._current_endpoint_ticket = Mock(return_value="new-ticket")
        self.service._emit_ready_status = Mock()
        self.service._emit_invite = AsyncMock()
        self.service._invite_requested = True
        await self.service._refresh_online_route(7)
        self.assertTrue(self.service._relay_ready)
        self.service._emit_invite.assert_awaited_once()


    async def test_start_room_covers_cancelled_open_and_new_endpoint_activation(self) -> None:
        self.store.iroh_room_secret.return_value = bytes(range(32))
        self.service._stop_endpoint = AsyncMock()
        self.service._open_room_endpoint = AsyncMock(return_value=None)
        fake_iroh = SimpleNamespace()
        with patch.dict(sys.modules, {"iroh": fake_iroh}):
            await self.service._start_room("new-room", emit_invite=False)
        self.service._open_room_endpoint.return_value = (Mock(), True)
        self.service._activate_room_endpoint = Mock()
        self.service._emit_invite = AsyncMock()
        with patch.dict(sys.modules, {"iroh": fake_iroh}):
            await self.service._start_room("new-room", emit_invite=True)
        self.service._activate_room_endpoint.assert_called_once()
        self.service._emit_invite.assert_awaited_once()

    async def test_join_rejects_ticket_and_connection_identity_then_publishes_success(self) -> None:
        invite = SimpleNamespace(
            room_id="room-identifier", endpoint_ticket="ticket", endpoint_id="expected"
        )
        self.service._start_room = AsyncMock()
        self.service._emit_details = Mock()
        connection = SimpleNamespace(remote_id=Mock(return_value="remote"), close=Mock())
        endpoint = SimpleNamespace(connect=AsyncMock(return_value=connection))
        self.service._endpoint = endpoint
        self.service._stop_endpoint = AsyncMock()
        ticket = Mock()
        ticket.endpoint_addr.return_value.id.return_value.__str__ = Mock(return_value="changed")
        fake_iroh = SimpleNamespace(
            EndpointTicket=SimpleNamespace(from_string=Mock(return_value=ticket))
        )
        with patch.dict(sys.modules, {"iroh": fake_iroh}), self.assertRaisesRegex(
            IrohProtocolError, "ticket identity changed"
        ):
            await self.service._join_room(invite)
        self.service._stop_endpoint.assert_awaited_once()

        ticket.endpoint_addr.return_value.id.return_value.__str__ = Mock(return_value="expected")
        self.service._stop_endpoint.reset_mock()
        with patch.dict(sys.modules, {"iroh": fake_iroh}), self.assertRaisesRegex(
            IrohProtocolError, "does not match"
        ):
            await self.service._join_room(invite)
        connection.close.assert_called_with(1, b"ticket identity mismatch")

        connection.remote_id = Mock(return_value="expected")
        self.service._exchange = AsyncMock()
        self.store.activate_joined_iroh_room.reset_mock()
        with patch.dict(sys.modules, {"iroh": fake_iroh}):
            await self.service._join_room(invite)
        self.store.activate_joined_iroh_room.assert_called_once_with("room-identifier")

    async def test_resume_rejects_missing_routes_and_tries_peers_until_success(self) -> None:
        self.service._start_room = AsyncMock()
        self.service._emit_details = Mock()
        self.service._stop_endpoint = AsyncMock()
        self.store.iroh_peers.return_value = []
        with self.assertRaisesRegex(IrohProtocolError, "no saved peer"):
            await self.service._resume_join("room-identifier")
        peer = {"endpointTicket": "ticket", "endpointId": "expected"}
        self.store.iroh_peers.return_value = [peer]
        fake_iroh = SimpleNamespace(
            EndpointTicket=SimpleNamespace(from_string=Mock(side_effect=ValueError("bad")))
        )
        with patch.dict(sys.modules, {"iroh": fake_iroh}), self.assertRaisesRegex(
            IrohProtocolError, "could not resume"
        ):
            await self.service._resume_join("room-identifier")
        self.service._stop_endpoint.assert_awaited_once()

        ticket = Mock()
        connection = SimpleNamespace(remote_id=Mock(return_value="expected"))
        ticket.endpoint_addr.return_value = Mock()
        fake_iroh.EndpointTicket.from_string = Mock(return_value=ticket)
        self.service._endpoint = SimpleNamespace(connect=AsyncMock(return_value=connection))
        self.service._exchange = AsyncMock()
        with patch.dict(sys.modules, {"iroh": fake_iroh}):
            await self.service._resume_join("room-identifier")
        self.store.activate_joined_iroh_room.assert_called_with("room-identifier")

    async def test_serve_requests_exits_on_idle_and_closes_on_handler_failure(self) -> None:
        connection = SimpleNamespace(
            close_reason=Mock(return_value=None),
            accept_bi=AsyncMock(side_effect=TimeoutError),
            close=Mock(),
        )
        await self.service._serve_requests(connection, self.service._generation)
        connection.accept_bi.side_effect = None
        connection.accept_bi.return_value = Mock()
        self.service._handle_request = AsyncMock(side_effect=ValueError("bad request"))
        await self.service._serve_requests(connection, self.service._generation)
        connection.close.assert_called_with(0, b"connection ended")

    async def test_perform_hello_rejects_mismatch_and_saves_matching_peer(self) -> None:
        connection = SimpleNamespace(remote_id=Mock(return_value="peer"))
        self.service._request_id = Mock(return_value="request")
        self.service._local_hello = Mock(return_value={"kind": "hello"})
        self.service._request = AsyncMock(return_value={"kind": "inventoryResult", "requestId": "request"})
        with self.assertRaisesRegex(IrohProtocolError, "matching hello"):
            await self.service._perform_hello(connection)
        self.service._request.return_value = {"kind": "hello", "requestId": "request"}
        self.service._validate_hello = Mock()
        self.service._save_peer = Mock()
        await self.service._perform_hello(connection)
        self.service._save_peer.assert_called_once()

    async def test_pull_rejects_wrong_inventory_and_nonadvancing_cursor(self) -> None:
        self.service._request = AsyncMock(return_value={"kind": "hello"})
        with self.assertRaisesRegex(IrohProtocolError, "wrong inventory"):
            await self.service._pull_until_converged(Mock())
        self.service._request.return_value = {
            "kind": "inventoryResult",
            "entries": [{"domain": "task", "id": "operation-a", "digest": "digest"}],
            "next": "wrong",
        }
        self.store.missing_iroh_references.return_value = []
        with self.assertRaisesRegex(IrohProtocolError, "cursor"):
            await self.service._pull_until_converged(Mock())
        self.service._request.return_value = {
            "kind": "inventoryResult", "entries": [], "next": None
        }
        await self.service._pull_until_converged(Mock())

    async def test_handle_incoming_requires_hello_before_exchange(self) -> None:
        stream = SimpleNamespace(recv=Mock(), send=Mock())
        connection = SimpleNamespace(
            accept_bi=AsyncMock(return_value=stream), remote_id=Mock(return_value="peer"), close=Mock()
        )
        self.service._read_message = AsyncMock(return_value={"kind": "inventory"})
        await self.service._handle_incoming(connection, self.service._generation)
        connection.close.assert_called_with(1, b"hello required")
        self.service._read_message.return_value = {"kind": "hello", "requestId": "r"}
        self.service._validate_hello = Mock()
        self.service._save_peer = Mock()
        self.service._write_message = AsyncMock()
        self.service._local_hello = Mock(return_value={"kind": "hello"})
        self.service._exchange_after_hello = AsyncMock()
        await self.service._handle_incoming(connection, self.service._generation)
        self.service._exchange_after_hello.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
