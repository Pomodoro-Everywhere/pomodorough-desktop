from __future__ import annotations

import asyncio
import unittest

from pomodorough.iroh_network import EndpointKeyStore, IrohService
from pomodorough.iroh_protocol import ALPN, decode_frame, encode_frame


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

    def test_hello_omits_machine_name(self) -> None:
        service = IrohService.__new__(IrohService)
        service.device_id = "device-12345678"
        service._room_id = "room-id"
        service._room_secret = bytes(range(32))
        service._endpoint_ticket = "endpoint-ticket"
        service._current_endpoint_ticket = lambda: "endpoint-ticket"

        hello = service._local_hello("018f47f3-7b5c-7000-8000-000000000001")

        self.assertNotIn("displayName", hello)


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
