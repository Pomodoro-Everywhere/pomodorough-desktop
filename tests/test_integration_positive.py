import unittest

from iroh_protocol_cases import (
    DEVICE_ID,
    OPERATION_ID,
    ROOM_ID,
    SECRET,
    auto_start_record,
    duration_record,
    envelope,
    genesis_record,
    selected_task_record,
    task_record,
    timer_record,
)

from pomodorough.iroh_protocol import (
    create_invite,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
    parse_invite,
    record_digest,
    validate_message,
    validate_record,
)


class PositiveIntegrationTests(unittest.TestCase):
    def assert_message_roundtrip(self, message: dict[str, object]) -> None:
        body = encode_message(message)
        received = decode_frame(encode_frame(body, SECRET), SECRET)
        self.assertEqual(decode_message(received), message)

    def test_authenticated_frame_carries_a_roundtrippable_room_invite(self) -> None:
        invite = create_invite(
            SECRET,
            "endpoint-integration-ticket",
            "Focus room",
            ticket_parser=lambda _ticket: "endpoint-integration-id",
        )

        received = decode_frame(encode_frame(invite.encode(), SECRET), SECRET).decode()
        parsed = parse_invite(
            received,
            ticket_parser=lambda _ticket: "endpoint-integration-id",
        )

        self.assertEqual(parsed.room_secret, SECRET)
        self.assertEqual(parsed.room_name, "Focus room")
        self.assertEqual(parsed.endpoint_id, "endpoint-integration-id")

    def test_all_message_kinds_survive_authenticated_wire_roundtrip(self) -> None:
        record = auto_start_record()
        messages = (
            envelope(
                "hello",
                deviceId=DEVICE_ID,
                endpointTicket="endpoint-ticket",
                platform="linux",
                displayName="Desk peer",
            ),
            envelope("inventory", after=None, limit=1024),
            envelope(
                "inventoryResult",
                entries=[
                    {
                        "domain": "autoStart",
                        "id": OPERATION_ID,
                        "digest": record_digest(record),
                    }
                ],
                next=f"autoStart\0{OPERATION_ID}",
            ),
            envelope(
                "operations",
                refs=[{"domain": "autoStart", "id": OPERATION_ID}],
            ),
            envelope("operationsResult", records=[record]),
            envelope(
                "error",
                code="not_found",
                message="record unavailable",
                retryable=False,
            ),
        )

        for message in messages:
            with self.subTest(kind=message["kind"]):
                self.assert_message_roundtrip(message)

    def test_all_supported_operation_domains_validate_together(self) -> None:
        records = (
            genesis_record(),
            timer_record(),
            task_record(),
            duration_record(),
            auto_start_record(),
            selected_task_record(),
        )
        response = envelope("operationsResult", records=list(records))

        self.assertIs(validate_message(response), response)
        for record in records:
            with self.subTest(domain=record["domain"]):
                self.assertIs(validate_record(record), record)

    def test_inventory_accepts_empty_terminal_page_and_genesis_cursor(self) -> None:
        request = envelope("inventory", after="genesis\0genesis", limit=1)
        result = envelope("inventoryResult", entries=[], next=None)

        self.assertEqual(decode_message(encode_message(request)), request)
        self.assertEqual(decode_message(encode_message(result)), result)
        self.assertEqual(request["roomId"], ROOM_ID)


if __name__ == "__main__":
    unittest.main()
