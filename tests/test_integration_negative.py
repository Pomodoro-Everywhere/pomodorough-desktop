import json
import unittest

from pomodorough.iroh_protocol import (
    INVITE_PREFIX,
    IrohProtocolError,
    b64url_encode,
    decode_frame,
    encode_frame,
    parse_invite,
    room_id_for_secret,
)


class NegativeIntegrationTests(unittest.TestCase):
    def test_authenticated_frame_does_not_bypass_invite_room_binding(self) -> None:
        secret = bytes(range(32))
        document = {
            "v": 1,
            "roomId": room_id_for_secret(bytes(reversed(range(32)))),
            "endpointTicket": "endpoint-integration-ticket",
            "roomSecret": b64url_encode(secret),
        }
        invite = INVITE_PREFIX + b64url_encode(
            json.dumps(document, separators=(",", ":")).encode()
        )
        received = decode_frame(encode_frame(invite.encode(), secret), secret).decode()

        with self.assertRaisesRegex(IrohProtocolError, "does not match"):
            parse_invite(
                received,
                ticket_parser=lambda _ticket: "endpoint-integration-id",
            )


if __name__ == "__main__":
    unittest.main()
