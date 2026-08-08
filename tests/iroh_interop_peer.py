from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pomodorough.iroh_protocol import (
    ALPN,
    MAX_FRAME_BODY,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
    room_id_for_secret,
)


async def serve(ticket_file: Path) -> None:
    import iroh

    iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())
    endpoint = await iroh.Endpoint.bind(
        iroh.EndpointOptions(
            preset=iroh.preset_n0(),
            alpns=[ALPN],
            relay_mode=iroh.RelayMode.disabled(),
        )
    )
    ticket = str(iroh.EndpointTicket.from_addr(endpoint.addr()))
    ticket_file.write_text(ticket, encoding="utf-8")
    secret = bytes(range(32))
    try:
        incoming = await asyncio.wait_for(endpoint.accept_next(), timeout=30)
        if incoming is None:
            raise RuntimeError("Iroh endpoint closed before accepting peer.")
        connection = await asyncio.wait_for(
            (await incoming.accept()).connect(), timeout=30
        )
        stream = await asyncio.wait_for(connection.accept_bi(), timeout=30)
        frame = await asyncio.wait_for(
            stream.recv().read_to_end(MAX_FRAME_BODY + 36), timeout=30
        )
        message = decode_message(decode_frame(frame, secret))
        if (
            message["kind"] != "hello"
            or message["roomId"] != room_id_for_secret(secret)
            or message["platform"] != "macos"
        ):
            raise RuntimeError("Swift peer sent unexpected hello.")
        response = {
            "protocolVersion": 1,
            "roomId": message["roomId"],
            "requestId": message["requestId"],
            "kind": "hello",
            "deviceId": "device-python01",
            "endpointTicket": ticket,
            "platform": "linux",
        }
        await stream.send().write_all(encode_frame(encode_message(response), secret))
        await stream.send().finish()
        connection.close(0, b"interop complete")
    finally:
        await endpoint.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket-file", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.ticket_file))


if __name__ == "__main__":
    main()
