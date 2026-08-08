from __future__ import annotations

import json
import subprocess
import sys
import unittest

from pomodorough.iroh_protocol import (
    INVITE_PREFIX,
    IrohProtocolError,
    b64url_encode,
    create_invite,
    decode_frame,
    encode_frame,
    parse_invite,
    record_digest,
    room_id_for_secret,
)
from pomodorough.storage import utc_timestamp


class IrohProtocolTests(unittest.TestCase):
    def test_room_id_vector(self) -> None:
        self.assertEqual(
            room_id_for_secret(bytes(range(32))),
            "Z_qLtnvZQsi-d2Giw1lvj7yy1x20hyE4jUgODkFsQBs",
        )

    def test_invite_strict_roundtrip(self) -> None:
        secret = bytes(range(32))
        invite = create_invite(
            secret,
            "endpoint-test-ticket",
            "Design desk",
            ticket_parser=lambda ticket: "endpoint-id" if ticket else "",
        )
        parsed = parse_invite(
            invite, ticket_parser=lambda ticket: "endpoint-id" if ticket else ""
        )

        self.assertEqual(parsed.room_secret, secret)
        self.assertEqual(parsed.room_name, "Design desk")
        self.assertEqual(parsed.endpoint_id, "endpoint-id")
        self.assertEqual(
            parsed.room_id,
            "Z_qLtnvZQsi-d2Giw1lvj7yy1x20hyE4jUgODkFsQBs",
        )

    def test_invite_rejects_unknown_duplicate_and_mismatched_fields(self) -> None:
        secret = bytes(range(32))
        room_id = room_id_for_secret(secret)
        cases = [
            {
                "v": 1,
                "roomId": room_id,
                "endpointTicket": "endpoint-ticket",
                "roomSecret": b64url_encode(secret),
                "unknown": True,
            },
            {
                "v": 1,
                "roomId": room_id_for_secret(bytes(reversed(range(32)))),
                "endpointTicket": "endpoint-ticket",
                "roomSecret": b64url_encode(secret),
            },
            {
                "v": True,
                "roomId": room_id,
                "endpointTicket": "endpoint-ticket",
                "roomSecret": b64url_encode(secret),
            },
        ]
        for document in cases:
            with self.subTest(document=document):
                invite = INVITE_PREFIX + b64url_encode(
                    json.dumps(document, separators=(",", ":")).encode()
                )
                with self.assertRaises(IrohProtocolError):
                    parse_invite(invite, ticket_parser=lambda _ticket: "endpoint-id")

        duplicate = (
            '{"v":1,"v":1,"roomId":"'
            + room_id
            + '","endpointTicket":"endpoint-ticket","roomSecret":"'
            + b64url_encode(secret)
            + '"}'
        )
        with self.assertRaises(IrohProtocolError):
            parse_invite(
                INVITE_PREFIX + b64url_encode(duplicate.encode()),
                ticket_parser=lambda _ticket: "endpoint-id",
            )

    def test_hmac_frame_rejects_tampering_and_trailing_bytes(self) -> None:
        secret = bytes(range(32))
        frame = encode_frame(b'{"kind":"hello"}', secret)
        self.assertEqual(decode_frame(frame, secret), b'{"kind":"hello"}')

        for changed in (
            frame[:8] + bytes([frame[8] ^ 1]) + frame[9:],
            frame[:-1] + bytes([frame[-1] ^ 1]),
            frame + b"x",
        ):
            with self.subTest(changed=changed[-4:]):
                with self.assertRaises(IrohProtocolError):
                    decode_frame(changed, secret)

    def test_frame_and_canonical_digest_match_cross_client_vectors(self) -> None:
        secret = bytes(range(32))
        self.assertEqual(
            encode_frame(b'{"kind":"hello"}', secret).hex(),
            "00000010d9f01510c6ce30066f8318494a013c47657387a9bc3bbb81625b3cd74569d8377b226b696e64223a2268656c6c6f227d",
        )
        record = {
            "domain": "autoStart",
            "deviceId": "device-test0001",
            "operation": {
                "id": "auto-start-operation-peer0001",
                "enabled": True,
                "occurredAt": "1970-01-01T00:16:40Z",
                "hlcWallMs": 1_000_000,
                "hlcCounter": 0,
            },
        }
        self.assertEqual(
            record_digest(record),
            "ViRTrF---kkCpXCRyxUvXbeZSas4Iyal_dtSbi4TTzE",
        )

    def test_auto_start_wire_record_uses_wrapper_device(self) -> None:
        record = {
            "domain": "autoStart",
            "deviceId": "device-alpha",
            "operation": {
                "id": "operation-alpha",
                "enabled": True,
                "occurredAt": utc_timestamp(1_786_000_000_000),
                "hlcWallMs": 1_786_000_000_000,
                "hlcCounter": 0,
            },
        }
        self.assertEqual(len(record_digest(record)), 43)
        record["operation"]["deviceId"] = "device-alpha"
        with self.assertRaises(IrohProtocolError):
            record_digest(record)

    def test_cli_and_tui_import_without_iroh(self) -> None:
        script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'iroh' or name.startswith('iroh.'):
        raise ImportError('iroh deliberately unavailable')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import pomodorough.cli, pomodorough.tui, pomodorough.terminal
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
