from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.iroh_network import EndpointKeyStore, IrohService
from pomodorough.iroh_protocol import (
    ALPN,
    IrohProtocolError,
    decode_frame,
    encode_frame,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value


class EndpointKeyStoreTests(unittest.TestCase):
    def test_key_is_stable_in_secure_store(self) -> None:
        secrets = MemorySecretStore()
        store = EndpointKeyStore(secrets)
        first = store.load_or_create()
        second = store.load_or_create()

        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual(secrets.values["endpoint-key-v1"], first)

    def test_malformed_saved_key_fails_closed_without_replacing_it(self) -> None:
        secrets = MemorySecretStore()
        secrets.values["endpoint-key-v1"] = b"short"

        with self.assertRaisesRegex(IrohProtocolError, "identity is invalid"):
            EndpointKeyStore(secrets).load_or_create()

        self.assertEqual(secrets.values["endpoint-key-v1"], b"short")

    def test_hello_omits_machine_name(self) -> None:
        service = IrohService.__new__(IrohService)
        service.device_id = "device-12345678"
        service._room_id = "room-id"
        service._room_secret = bytes(range(32))
        service._endpoint_ticket = "endpoint-ticket"
        service._current_endpoint_ticket = lambda: "endpoint-ticket"

        hello = service._local_hello("018f47f3-7b5c-7000-8000-000000000001")

        self.assertNotIn("displayName", hello)


class IrohServiceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = IrohService(
            Path(self.temporary.name) / "state.sqlite3",
            "device-12345678",
            key_store=EndpointKeyStore(MemorySecretStore()),
        )

    def tearDown(self) -> None:
        self.service.shutdown()
        self.temporary.cleanup()

    def test_lifecycle_reports_missing_room_and_worker_dependencies(self) -> None:
        failures: list[str] = []
        statuses: list[str] = []
        self.service.failure.connect(failures.append)
        self.service.status_changed.connect(statuses.append)

        self.assertFalse(self.service.running)
        self.service.refresh_invite()
        self.service.stop()
        self.assertEqual(failures, ["No Iroh room is active."])
        self.assertEqual(statuses, ["NOT CONNECTED"])

        with self.assertRaisesRegex(IrohProtocolError, "storage worker"):
            self.service._required_store()
        with self.assertRaisesRegex(IrohProtocolError, "endpoint is not running"):
            self.service._required_endpoint()
        with self.assertRaisesRegex(IrohProtocolError, "endpoint is not ready"):
            self.service._required_context()

        endpoint = object()
        store = object()
        self.service._endpoint = endpoint
        self.service._store = store  # type: ignore[assignment]
        self.service._room_id = "room-1"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "ticket-1"
        self.assertTrue(self.service.running)
        self.assertIs(self.service._required_endpoint(), endpoint)
        self.assertIs(self.service._required_store(), store)
        self.assertEqual(
            self.service._required_context(),
            ("room-1", bytes(range(32)), "ticket-1"),
        )
        self.service._closing = True
        self.assertFalse(self.service.running)

    def test_protocol_envelopes_bound_peer_visible_errors_and_platform_names(self) -> None:
        self.service._room_id = "room-1"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "ticket-1"

        envelope = self.service._envelope("request-1", "inventory", records=[])
        self.assertEqual(envelope["roomId"], "room-1")
        self.assertEqual(envelope["requestId"], "request-1")
        self.assertEqual(envelope["kind"], "inventory")
        self.assertEqual(envelope["records"], [])

        internal = self.service._error(
            {"requestId": "request-2"}, "internal", "🙂" * 2_000
        )
        self.assertLessEqual(len(internal["message"].encode("utf-8")), 1024)
        self.assertTrue(internal["retryable"])
        rejected = self.service._error(
            {"requestId": "request-3"}, "invalid_request", "bad request"
        )
        self.assertFalse(rejected["retryable"])

        platforms = {"darwin": "macos", "linux": "linux", "linux2": "linux", "win32": "windows"}
        for platform, expected in platforms.items():
            with self.subTest(platform=platform), patch(
                "pomodorough.iroh_network.sys.platform", platform
            ):
                self.assertEqual(IrohService._platform_name(), expected)
        with (
            patch("pomodorough.iroh_network.sys.platform", "plan9"),
            self.assertRaisesRegex(IrohProtocolError, "unavailable"),
        ):
            IrohService._platform_name()

    def test_serialized_operations_require_and_use_the_session_lock(self) -> None:
        async def value() -> str:
            return "complete"

        async def scenario() -> None:
            with self.assertRaisesRegex(IrohProtocolError, "event loop is not ready"):
                await self.service._serialized(value())
            self.service._session_lock = asyncio.Lock()
            self.assertEqual(await self.service._serialized(value()), "complete")

        asyncio.run(scenario())


@unittest.skipUnless(IrohService.availability()[0], IrohService.availability()[1])
class IrohLoopbackTests(unittest.TestCase):
    def test_actual_localhost_authenticated_frame_roundtrip(self) -> None:
        asyncio.run(self._roundtrip())

    async def _roundtrip(self) -> None:
        import iroh

        iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
        server = await iroh.Endpoint.bind(
            iroh.EndpointOptions(
                preset=iroh.preset_n0(),
                alpns=[ALPN],
                relay_mode=iroh.RelayMode.disabled(),
            )
        )
        client = await iroh.Endpoint.bind(
            iroh.EndpointOptions(
                preset=iroh.preset_n0(), relay_mode=iroh.RelayMode.disabled()
            )
        )
        secret = bytes(range(32))
        received = asyncio.Event()

        async def serve() -> None:
            incoming = await server.accept_next()
            connection = await (await incoming.accept()).connect()
            stream = await connection.accept_bi()
            body = decode_frame(await stream.recv().read_to_end(1024), secret)
            await stream.send().write_all(encode_frame(body, secret))
            await stream.send().finish()
            await received.wait()

        server_task = asyncio.create_task(serve())
        connection = await client.connect(server.addr(), ALPN)
        stream = await connection.open_bi()
        await stream.send().write_all(encode_frame(b"iroh-localhost", secret))
        await stream.send().finish()
        echoed = decode_frame(await stream.recv().read_to_end(1024), secret)
        received.set()

        self.assertEqual(echoed, b"iroh-localhost")
        connection.close(0, b"done")
        await server_task
        await client.close()
        await server.close()


if __name__ == "__main__":
    unittest.main()
