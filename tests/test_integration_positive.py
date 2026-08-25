import unittest

from pomodorough.iroh_protocol import (
    create_invite,
    decode_frame,
    encode_frame,
    parse_invite,
)


class PositiveIntegrationTests(unittest.TestCase):
    def test_authenticated_frame_carries_a_roundtrippable_room_invite(self) -> None:
        secret = bytes(range(32))
        invite = create_invite(
            secret,
            "endpoint-integration-ticket",
            "Focus room",
            ticket_parser=lambda _ticket: "endpoint-integration-id",
        )

        received = decode_frame(encode_frame(invite.encode(), secret), secret).decode()
        parsed = parse_invite(
            received,
            ticket_parser=lambda _ticket: "endpoint-integration-id",
        )

        self.assertEqual(parsed.room_secret, secret)
        self.assertEqual(parsed.room_name, "Focus room")
        self.assertEqual(parsed.endpoint_id, "endpoint-integration-id")


if __name__ == "__main__":
    unittest.main()
