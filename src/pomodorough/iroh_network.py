from __future__ import annotations

import asyncio
import random
import secrets
import sys
import threading
import time
from concurrent.futures import CancelledError, Future
from pathlib import Path
from typing import Any, Coroutine

from PySide6.QtCore import QObject, Signal

from .iroh_protocol import (
    ALPN,
    MAX_FRAME_BODY,
    MAX_INVENTORY,
    MAX_OPERATION_REFS,
    PROTOCOL_VERSION,
    IrohProtocolError,
    ImmutableConflict,
    RoomInvite,
    create_invite,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
    parse_invite,
    valid_identifier,
)
from .storage import Store
from .secure_store import PlatformSecretStore
from .uuid7 import reserve_uuid7


class EndpointKeyStore:
    def __init__(self, secret_store: PlatformSecretStore | None = None) -> None:
        self.secret_store = secret_store or PlatformSecretStore()
        self.key = "endpoint-key-v1"

    def load_or_create(self) -> bytes:
        secret = self.secret_store.load(self.key)
        if secret is None:
            secret = secrets.token_bytes(32)
            self.secret_store.save(self.key, secret)
        if len(secret) != 32:
            raise IrohProtocolError("Saved Iroh endpoint identity is invalid.")
        return secret


class IrohService(QObject):
    MAX_PENDING_HANDSHAKES = 16
    MAX_AUTHENTICATED_CONNECTIONS = 8
    HANDSHAKE_TIMEOUT = 10
    REQUEST_TIMEOUT = 30
    CONNECTION_IDLE_TIMEOUT = 2
    status_changed = Signal(str)
    details_changed = Signal(object)
    invite_ready = Signal(str)
    joined = Signal()
    projection_changed = Signal()
    failure = Signal(str)

    def __init__(
        self,
        database_path: Path,
        device_id: str,
        *,
        key_store: EndpointKeyStore | None = None,
    ) -> None:
        super().__init__()
        self.database_path = Path(database_path)
        self.device_id = device_id
        self.key_store = key_store or EndpointKeyStore()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._endpoint: Any = None
        self._store: Store | None = None
        self._room_id: str | None = None
        self._room_secret: bytes | None = None
        self._endpoint_ticket: str | None = None
        self._accept_task: asyncio.Task[Any] | None = None
        self._periodic_task: asyncio.Task[Any] | None = None
        self._connection_tasks: set[asyncio.Task[Any]] = set()
        self._operations: set[Future[Any]] = set()
        self._generation = 0
        self._closing = False
        self._relay_ready = False
        self._invite_requested = False
        self._session_lock: asyncio.Lock | None = None
        self._authenticated_connections = 0

    @staticmethod
    def availability() -> tuple[bool, str]:
        try:
            import iroh  # noqa: F401 - verifies native wheel loadability
        except (ImportError, OSError) as error:
            return (
                False,
                "Iroh support unavailable: install optional dependency iroh==1.1.0 "
                f"for a supported platform ({error}).",
            )
        return True, "Iroh 1.1.0 ready"

    @staticmethod
    def _platform_name() -> str:
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        if sys.platform == "win32":
            return "windows"
        raise IrohProtocolError(
            "Iroh synchronization protocol v1 is unavailable on this operating system."
        )

    @property
    def running(self) -> bool:
        return self._endpoint is not None and not self._closing

    def start_room(self, room_id: str, *, emit_invite: bool = False) -> None:
        self._submit(
            self._serialized(self._start_room(room_id, emit_invite=emit_invite)),
            tracked=True,
        )

    def join_room(self, invite: RoomInvite) -> None:
        self._submit(self._serialized(self._join_room(invite)), tracked=True)

    def resume_join(self, room_id: str) -> None:
        self._submit(self._serialized(self._resume_join(room_id)), tracked=True)

    def refresh_invite(self) -> None:
        if self._room_id is None:
            self.failure.emit("No Iroh room is active.")
            return
        self._submit(self._emit_invite(), tracked=True)

    def sync_now(self) -> None:
        self._submit(self._serialized(self._sync_known_peers()), tracked=True)

    def stop(self) -> None:
        if self._loop is None:
            self.status_changed.emit("NOT CONNECTED")
            return
        self._cancel_operations()
        self._submit(self._stop_endpoint())

    def shutdown(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        self._cancel_operations()
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            future.result(timeout=10)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        self._thread = None
        self._loop = None
        self._ready.clear()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        available, reason = self.availability()
        if not available:
            self.status_changed.emit("UNAVAILABLE")
            self.failure.emit(reason)
            raise IrohProtocolError(reason)
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="pomodorough-iroh",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10) or self._loop is None:
            raise IrohProtocolError("Iroh event loop did not start.")
        return self._loop

    def _run_loop(self) -> None:
        import iroh

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        iroh.iroh_ffi.uniffi_set_event_loop(loop)
        self._loop = loop
        self._store = Store(self.database_path)
        self._session_lock = asyncio.Lock()
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            if self._store is not None:
                self._store.close()
                self._store = None
            self._session_lock = None
            loop.close()

    def _submit(
        self, coroutine: Coroutine[Any, Any, Any], *, tracked: bool = False
    ) -> Future[Any] | None:
        try:
            loop = self._ensure_loop()
        except IrohProtocolError:
            coroutine.close()
            return None
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        if tracked:
            self._operations.add(future)

        def completed(result: Future[Any]) -> None:
            self._operations.discard(result)
            try:
                result.result()
            except (asyncio.CancelledError, CancelledError):
                return
            except Exception as error:
                self.status_changed.emit("UNAVAILABLE")
                self.failure.emit(str(error))

        future.add_done_callback(completed)
        return future

    def _cancel_operations(self) -> None:
        for future in tuple(self._operations):
            future.cancel()
        self._operations.clear()

    async def _start_room(self, room_id: str, *, emit_invite: bool) -> None:
        import iroh

        store = self._required_store()
        room_secret = store.iroh_room_secret(room_id)
        if (
            self._endpoint is not None
            and self._room_id == room_id
            and not self._endpoint.is_closed()
        ):
            if emit_invite:
                await self._emit_invite()
            return
        await self._stop_endpoint()
        owner = self._generation
        opened = await self._open_room_endpoint(iroh, owner)
        if opened is None:
            return
        endpoint, relay_ready = opened
        self._activate_room_endpoint(
            iroh, endpoint, room_id, room_secret, relay_ready
        )
        if emit_invite:
            await self._emit_invite()

    async def _open_room_endpoint(
        self, iroh: Any, owner: int
    ) -> tuple[Any, bool] | None:
        self.status_changed.emit("OPENING ROUTE")
        key = self.key_store.load_or_create()
        endpoint = await iroh.Endpoint.bind(
            iroh.EndpointOptions(
                preset=iroh.preset_n0(),
                secret_key=key,
                alpns=[ALPN],
            )
        )
        if owner != self._generation or self._closing:
            await endpoint.close()
            return None
        try:
            try:
                async with asyncio.timeout(5):
                    await endpoint.online()
            except TimeoutError:
                relay_ready = False
            else:
                relay_ready = True
        except asyncio.CancelledError:
            await endpoint.close()
            raise
        if owner != self._generation or self._closing:
            await endpoint.close()
            return None
        return endpoint, relay_ready

    def _activate_room_endpoint(
        self, iroh: Any, endpoint: Any, room_id: str,
        room_secret: bytes, relay_ready: bool,
    ) -> None:
        self._endpoint = endpoint
        self._room_id = room_id
        self._room_secret = room_secret
        self._endpoint_ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))
        self._relay_ready = relay_ready
        self._generation += 1
        generation = self._generation
        self._accept_task = asyncio.create_task(self._accept_loop(generation))
        self._periodic_task = asyncio.create_task(self._periodic_sync(generation))
        self._emit_ready_status()
        self._emit_details()

    async def _stop_endpoint(self) -> None:
        self._generation += 1
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._accept_task, self._periodic_task)
            if task and task is not current
        ]
        tasks.extend(self._connection_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._accept_task = None
        self._periodic_task = None
        self._connection_tasks.clear()
        endpoint = self._endpoint
        self._endpoint = None
        self._endpoint_ticket = None
        self._room_id = None
        self._room_secret = None
        self._relay_ready = False
        self._invite_requested = False
        if endpoint is not None and not endpoint.is_closed():
            await endpoint.close()
        self.status_changed.emit("NOT CONNECTED")
        self.details_changed.emit({})

    async def _shutdown(self) -> None:
        self._closing = True
        await self._stop_endpoint()

    async def _emit_invite(self) -> None:
        self._invite_requested = True
        store = self._required_store()
        room_id, secret, _ticket = self._required_context()
        ticket = self._current_endpoint_ticket()
        room = store.iroh_room(room_id)
        if room is None:
            raise IrohProtocolError("Iroh room metadata is missing.")
        self.invite_ready.emit(
            create_invite(secret, ticket, room.get("roomName"))
        )

    async def _join_room(self, invite: RoomInvite) -> None:
        import iroh

        store = self._required_store()
        try:
            await self._start_room(invite.room_id, emit_invite=False)
            endpoint = self._required_endpoint()
            parsed = iroh.EndpointTicket.from_string(invite.endpoint_ticket)
            if str(parsed.endpoint_addr().id()) != invite.endpoint_id:
                raise IrohProtocolError("Invite endpoint ticket identity changed.")
            async with asyncio.timeout(30):
                connection = await endpoint.connect(parsed.endpoint_addr(), ALPN)
            if str(connection.remote_id()) != invite.endpoint_id:
                connection.close(1, b"ticket identity mismatch")
                raise IrohProtocolError("Connected endpoint does not match invite ticket.")
            await self._exchange(connection)
            store.activate_joined_iroh_room(invite.room_id)
        except Exception:
            await self._stop_endpoint()
            raise
        self.projection_changed.emit()
        self.joined.emit()
        self._emit_details()

    async def _resume_join(self, room_id: str) -> None:
        store = self._required_store()
        await self._start_room(room_id, emit_invite=False)
        peers = store.iroh_peers(room_id)
        if not peers:
            raise IrohProtocolError("Incomplete Iroh room has no saved peer route.")
        import iroh

        last_error: Exception | None = None
        for peer in peers:
            try:
                ticket = iroh.EndpointTicket.from_string(peer["endpointTicket"])
                async with asyncio.timeout(30):
                    connection = await self._required_endpoint().connect(
                        ticket.endpoint_addr(), ALPN
                    )
                if str(connection.remote_id()) != peer["endpointId"]:
                    raise IrohProtocolError("Connected peer identity changed.")
                await self._exchange(connection)
                store.activate_joined_iroh_room(room_id)
                self.projection_changed.emit()
                self.joined.emit()
                self._emit_details()
                return
            except Exception as error:
                last_error = error
        await self._stop_endpoint()
        raise IrohProtocolError(
            f"Incomplete Iroh room could not resume: {last_error or 'no reachable peer'}"
        )

    async def _accept_loop(self, generation: int) -> None:
        endpoint = self._required_endpoint()
        while generation == self._generation and not endpoint.is_closed():
            try:
                incoming = await endpoint.accept_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.1)
                continue
            if incoming is None:
                return
            if len(self._connection_tasks) >= self.MAX_PENDING_HANDSHAKES:
                try:
                    await incoming.ignore()
                except Exception:
                    pass
                continue
            task = asyncio.create_task(
                self._accept_incoming(incoming, generation)
            )
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_tasks.discard)

    async def _accept_incoming(self, incoming: Any, generation: int) -> None:
        connection = None
        try:
            async with asyncio.timeout(self.HANDSHAKE_TIMEOUT):
                connection = await (await incoming.accept()).connect()
            if generation != self._generation or connection.alpn() != ALPN:
                connection.close(1, b"wrong protocol")
                return
            if self._authenticated_connections >= self.MAX_AUTHENTICATED_CONNECTIONS:
                connection.close(1, b"connection limit")
                return
            self._authenticated_connections += 1
            try:
                await self._handle_incoming(connection, generation)
            finally:
                self._authenticated_connections -= 1
        except asyncio.CancelledError:
            raise
        except Exception:
            if connection is not None:
                connection.close(1, b"handshake failed")
            else:
                try:
                    await incoming.ignore()
                except Exception:
                    pass

    async def _handle_incoming(self, connection: Any, generation: int) -> None:
        secret = self._required_context()[1]
        async with asyncio.timeout(self.HANDSHAKE_TIMEOUT):
            hello_stream = await connection.accept_bi()
        hello = await self._read_message(hello_stream.recv(), secret, 32 * 1024)
        if hello["kind"] != "hello":
            connection.close(1, b"hello required")
            return
        self._validate_hello(hello, str(connection.remote_id()))
        self._save_peer(hello, str(connection.remote_id()))
        await self._write_message(
            self._local_hello(hello["requestId"]), hello_stream.send(), secret
        )
        await self._exchange_after_hello(connection, generation)

    async def _serve_requests(self, connection: Any, generation: int) -> None:
        while generation == self._generation and connection.close_reason() is None:
            try:
                async with asyncio.timeout(self.CONNECTION_IDLE_TIMEOUT):
                    stream = await connection.accept_bi()
                await self._handle_request(stream)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return
            except Exception:
                connection.close(0, b"connection ended")
                return

    async def _handle_request(self, stream: Any) -> None:
        room_id, secret, _ticket = self._required_context()
        try:
            request = await self._read_message(stream.recv(), secret)
        except IrohProtocolError:
            await stream.recv().stop(1)
            await stream.send().reset(1)
            return
        try:
            if request["roomId"] != room_id:
                response = self._error(request, "wrong_room", "Request names a different room.")
            elif request["kind"] == "inventory":
                entries, cursor = self._required_store().iroh_inventory(
                    room_id, request["after"], request["limit"]
                )
                response = self._envelope(
                    request["requestId"],
                    "inventoryResult",
                    entries=entries,
                    next=cursor,
                )
            elif request["kind"] == "operations":
                try:
                    records = self._required_store().iroh_operations(
                        room_id, request["refs"]
                    )
                except KeyError:
                    response = self._error(
                        request, "not_found", "Requested operation was not found."
                    )
                else:
                    response = self._envelope(
                        request["requestId"],
                        "operationsResult",
                        records=records,
                    )
            else:
                response = self._error(
                    request,
                    "invalid_request",
                    "Only inventory and operations requests are allowed after hello.",
                )
        except ImmutableConflict:
            response = self._error(
                request, "immutable_conflict", "Room requires immutable-ID repair."
            )
        except ValueError as error:
            response = self._error(request, "invalid_request", str(error))
        await self._write_message(response, stream.send(), secret)

    async def _periodic_sync(self, generation: int) -> None:
        delay = 2.0
        while generation == self._generation:
            await self._refresh_online_route(generation)
            success = await self._serialized(self._sync_known_peers())
            delay = 15.0 if success else min(60.0, delay * 2)
            await asyncio.sleep(min(60.0, delay + random.uniform(0, delay * 0.2)))

    async def _sync_known_peers(self) -> bool:
        if self._endpoint is None or self._room_id is None:
            return False
        try:
            self._required_store().capture_local_iroh_records()
        except ImmutableConflict:
            await self._stop_endpoint()
            self.status_changed.emit("REPAIR REQUIRED")
            self._emit_details()
            return False
        peers = self._required_store().iroh_peers(self._room_id)
        if not peers:
            self.status_changed.emit("WAITING FOR PEERS")
            self._emit_details()
            return True
        import iroh

        synchronized = False
        for peer in peers:
            try:
                ticket = iroh.EndpointTicket.from_string(peer["endpointTicket"])
                if str(ticket.endpoint_addr().id()) != peer["endpointId"]:
                    raise IrohProtocolError("Saved peer ticket identity changed.")
                self.status_changed.emit(
                    f"EXCHANGING · {ticket.endpoint_addr().id().fmt_short().upper()}"
                )
                async with asyncio.timeout(30):
                    connection = await self._endpoint.connect(ticket.endpoint_addr(), ALPN)
                if str(connection.remote_id()) != peer["endpointId"]:
                    raise IrohProtocolError("Connected peer identity changed.")
                await self._exchange(connection)
                synchronized = True
            except ImmutableConflict:
                await self._stop_endpoint()
                self.status_changed.emit("REPAIR REQUIRED")
                self._emit_details()
                return False
            except Exception:
                continue
        if synchronized:
            self._emit_ready_status()
        else:
            self.status_changed.emit("WAITING FOR PEERS")
        self._emit_details()
        return synchronized

    async def _perform_hello(self, connection: Any) -> None:
        request_id = self._request_id()
        response = await self._request(
            connection, self._local_hello(request_id)
        )
        if response["kind"] != "hello" or response["requestId"] != request_id:
            raise IrohProtocolError("Peer did not return matching hello.")
        self._validate_hello(response, str(connection.remote_id()))
        self._save_peer(response, str(connection.remote_id()))

    async def _pull_until_converged(self, connection: Any) -> None:
        room_id = self._required_context()[0]
        store = self._required_store()
        while True:
            changed = False
            cursor = None
            while True:
                inventory = await self._request(
                    connection,
                    self._envelope(
                        self._request_id(),
                        "inventory",
                        after=cursor,
                        limit=MAX_INVENTORY,
                    ),
                )
                if inventory["kind"] != "inventoryResult":
                    raise IrohProtocolError("Peer returned wrong inventory response.")
                missing = store.missing_iroh_references(
                    room_id, inventory["entries"]
                )
                for start in range(0, len(missing), MAX_OPERATION_REFS):
                    references = missing[start : start + MAX_OPERATION_REFS]
                    advertised = {
                        (entry["domain"], entry["id"]): entry["digest"]
                        for entry in inventory["entries"]
                        if (entry["domain"], entry["id"])
                        in {(item["domain"], item["id"]) for item in references}
                    }
                    if await self._fetch_records(
                        connection, store, room_id, references, advertised
                    ):
                        changed = True
                        self.projection_changed.emit()
                next_cursor = inventory["next"]
                if next_cursor is not None and (
                    next_cursor == cursor
                    or not inventory["entries"]
                    or next_cursor
                    != inventory["entries"][-1]["domain"]
                    + "\0"
                    + inventory["entries"][-1]["id"]
                ):
                    raise IrohProtocolError("Peer inventory cursor did not advance correctly.")
                cursor = next_cursor
                if cursor is None:
                    break
            if not changed:
                break

    async def _fetch_records(
        self,
        connection: Any,
        store: Store,
        room_id: str,
        references: list[dict[str, str]],
        advertised_digests: dict[tuple[str, str], str],
    ) -> bool:
        result = await self._request(
            connection,
            self._envelope(self._request_id(), "operations", refs=references),
        )
        if result["kind"] != "operationsResult":
            raise IrohProtocolError("Peer returned wrong operations response.")
        returned = {
            (
                record["domain"],
                "genesis"
                if record["domain"] == "genesis"
                else record["operation"]["id"],
            )
            for record in result["records"]
        }
        expected = {(item["domain"], item["id"]) for item in references}
        if len(result["records"]) != len(references) or returned != expected:
            raise IrohProtocolError(
                "Peer returned a partial or unrequested operation set."
            )
        return store.insert_remote_iroh_records(
            room_id, result["records"], advertised_digests
        )

    async def _request(self, connection: Any, message: dict[str, Any]) -> dict[str, Any]:
        secret = self._required_context()[1]
        async with asyncio.timeout(30):
            stream = await connection.open_bi()
            await self._write_message(message, stream.send(), secret)
            response = await self._read_message(
                stream.recv(),
                secret,
                32 * 1024 if message["kind"] == "hello" else MAX_FRAME_BODY,
            )
        if response["requestId"] != message["requestId"]:
            raise IrohProtocolError("Peer response request ID does not match.")
        if response["roomId"] != message["roomId"]:
            raise IrohProtocolError("Peer response names a different room.")
        if response["kind"] == "error":
            if response["code"] == "immutable_conflict":
                raise ImmutableConflict(response["message"])
            raise IrohProtocolError(response["message"])
        return response

    async def _read_message(
        self, stream: Any, secret: bytes, maximum: int = MAX_FRAME_BODY
    ) -> dict[str, Any]:
        async with asyncio.timeout(self.REQUEST_TIMEOUT):
            frame = await stream.read_to_end(maximum + 36)
        return decode_message(decode_frame(frame, secret))

    async def _write_message(
        self, message: dict[str, Any], stream: Any, secret: bytes
    ) -> None:
        async with asyncio.timeout(30):
            await stream.write_all(encode_frame(encode_message(message), secret))
            await stream.finish()

    def _local_hello(self, request_id: str) -> dict[str, Any]:
        return self._envelope(
            request_id,
            "hello",
            deviceId=self.device_id,
            endpointTicket=self._current_endpoint_ticket(),
            platform=self._platform_name(),
        )

    def _validate_hello(self, hello: dict[str, Any], remote_id: str) -> None:
        import iroh

        room_id = self._required_context()[0]
        if (
            hello["kind"] != "hello"
            or hello["roomId"] != room_id
            or not valid_identifier(hello["deviceId"])
        ):
            raise IrohProtocolError("Peer hello is invalid or names another room.")
        ticket = iroh.EndpointTicket.from_string(hello["endpointTicket"])
        if str(ticket.endpoint_addr().id()) != remote_id:
            raise IrohProtocolError("Peer hello ticket does not match Iroh identity.")

    def _save_peer(self, hello: dict[str, Any], remote_id: str) -> None:
        self._required_store().upsert_iroh_peer(
            self._required_context()[0],
            remote_id,
            hello["endpointTicket"],
            hello["deviceId"],
            hello.get("displayName"),
            int(time.time() * 1000),
        )
        self._emit_details()

    def _envelope(
        self, request_id: str, kind: str, **fields: Any
    ) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "roomId": self._required_context()[0],
            "requestId": request_id,
            "kind": kind,
            **fields,
        }

    def _error(
        self, request: dict[str, Any], code: str, message: str
    ) -> dict[str, Any]:
        encoded = message.encode("utf-8", errors="replace")[:1024]
        safe_message = encoded.decode("utf-8", errors="ignore")
        return self._envelope(
            request["requestId"],
            "error",
            code=code,
            message=safe_message,
            retryable=code == "internal",
        )

    def _request_id(self) -> str:
        return reserve_uuid7(int(time.time() * 1000), 1, None)[0]

    def _emit_details(self) -> None:
        if self._room_id is None or self._store is None:
            self.details_changed.emit({})
            return
        room = self._store.iroh_room(self._room_id) or {}
        room["peers"] = self._store.iroh_peers(self._room_id)
        self.details_changed.emit(room)

    def _required_store(self) -> Store:
        if self._store is None:
            raise IrohProtocolError("Iroh storage worker is not running.")
        return self._store

    def _required_endpoint(self) -> Any:
        if self._endpoint is None:
            raise IrohProtocolError("Iroh endpoint is not running.")
        return self._endpoint

    def _required_context(self) -> tuple[str, bytes, str]:
        if self._room_id is None or self._room_secret is None or self._endpoint_ticket is None:
            raise IrohProtocolError("Iroh room endpoint is not ready.")
        return self._room_id, self._room_secret, self._endpoint_ticket

    def _current_endpoint_ticket(self) -> str:
        import iroh

        endpoint = self._required_endpoint()
        ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))
        self._endpoint_ticket = ticket
        return ticket

    async def _refresh_online_route(self, generation: int) -> None:
        if self._relay_ready or self._endpoint is None:
            return
        try:
            async with asyncio.timeout(2):
                await self._endpoint.online()
        except TimeoutError:
            return
        if generation != self._generation or self._endpoint is None:
            return
        self._relay_ready = True
        self._current_endpoint_ticket()
        self._emit_ready_status()
        if self._invite_requested:
            await self._emit_invite()

    def _emit_ready_status(self) -> None:
        if self._endpoint is None:
            return
        route = "READY FOR PEERS" if self._relay_ready else "DIRECT ROUTE · RELAY WAITING"
        self.status_changed.emit(
            f"{route} · {self._endpoint.id().fmt_short().upper()}"
        )

    async def _exchange(self, connection: Any) -> None:
        try:
            await self._perform_hello(connection)
            await self._exchange_after_hello(connection, self._generation)
        finally:
            connection.close(0, b"sync complete")

    async def _exchange_after_hello(self, connection: Any, generation: int) -> None:
        serving = asyncio.create_task(self._serve_requests(connection, generation))
        try:
            await self._pull_until_converged(connection)
            await serving
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    async def _serialized(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        lock = self._session_lock
        if lock is None:
            coroutine.close()
            raise IrohProtocolError("Iroh event loop is not ready.")
        async with lock:
            return await coroutine


__all__ = [
    "EndpointKeyStore",
    "IrohService",
    "RoomInvite",
    "parse_invite",
]
