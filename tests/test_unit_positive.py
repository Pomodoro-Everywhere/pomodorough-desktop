import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from iroh_protocol_cases import (
    DEVICE_ID,
    OPERATION_ID,
    SECRET,
    TIMESTAMP_MS,
    auto_start_record,
    selected_task_record,
)

from pomodorough.iroh_protocol import (
    RoomInvite,
    b64url_decode,
    b64url_encode,
    canonical_json,
    create_invite,
    operation_order,
    record_digest,
    record_id,
    room_id_for_secret,
    valid_identifier,
    valid_request_id,
    valid_room_id,
    validate_record,
)
from pomodorough.storage_canonical_reconciliation import generated_break_day_bounds
from pomodorough.uuid7 import uuid7_from_parts


class PositiveUnitTests(unittest.TestCase):
    def test_generated_break_day_bounds_accept_unix_epoch_cross_platform(self) -> None:
        start, end = generated_break_day_bounds(0)

        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
        self.assertLessEqual(start_time.timestamp(), 0)
        self.assertGreater(end_time.timestamp(), 0)
        self.assertLessEqual((end_time - start_time).total_seconds(), 26 * 60 * 60)

    def test_canonical_json_orders_object_keys_by_utf16_code_units(self) -> None:
        value = {"\ue000": 1, "😀": 2}

        self.assertEqual(canonical_json(value), '{"😀":2,"":1}'.encode())

    def test_canonical_json_supports_every_portable_value_shape(self) -> None:
        value = {
            "none": None,
            "true": True,
            "false": False,
            "integer": -42,
            "string": "line\n😀",
            "list": [None, True, 0, "value"],
        }

        self.assertEqual(
            canonical_json(value),
            b'{"false":false,"integer":-42,"list":[null,true,0,"value"],'
            b'"none":null,"string":"line\\n\xf0\x9f\x98\x80","true":true}',
        )

    def test_base64url_and_identity_validators_accept_protocol_boundaries(self) -> None:
        encoded = b64url_encode(bytes(range(32)))
        request_id = uuid7_from_parts(TIMESTAMP_MS, 7)

        self.assertEqual(b64url_decode(encoded), bytes(range(32)))
        self.assertTrue(valid_room_id(room_id_for_secret(SECRET)))
        self.assertTrue(valid_request_id(request_id.upper()))
        for identifier in ("12345678", "device.with:allowed-chars_1"):
            with self.subTest(identifier=identifier):
                self.assertTrue(valid_identifier(identifier))

    def test_record_helpers_validate_digest_identify_and_order_operations(self) -> None:
        first = auto_start_record()
        second = selected_task_record()

        self.assertIs(validate_record(first), first)
        self.assertEqual(record_id(first), OPERATION_ID)
        self.assertEqual(len(b64url_decode(record_digest(first))), 32)
        self.assertLess(operation_order(first), operation_order(second))
        self.assertEqual(operation_order(first)[2], DEVICE_ID.encode())

    def test_native_ticket_identity_roundtrips_through_room_invite_encoding(self) -> None:
        endpoint_id = "endpoint-native-identifier"

        class Ticket:
            @staticmethod
            def endpoint_addr() -> object:
                return SimpleNamespace(id=lambda: endpoint_id)

        native = SimpleNamespace(
            EndpointTicket=SimpleNamespace(from_string=lambda _value: Ticket())
        )
        invite = RoomInvite(
            room_id_for_secret(SECRET),
            "native-endpoint-ticket",
            endpoint_id,
            SECRET,
        )
        with patch.dict(sys.modules, {"iroh": native}):
            encoded = invite.encode()

        self.assertTrue(encoded.startswith("pomodorough1."))
        self.assertEqual(
            create_invite(
                SECRET,
                "native-endpoint-ticket",
                ticket_parser=lambda _ticket: endpoint_id,
            ),
            encoded,
        )

    def test_legacy_auto_start_clock_is_accepted_only_with_epoch_sentinel(self) -> None:
        record = auto_start_record()
        record["operation"].update(
            occurredAt="1970-01-01T00:00:00.000Z", hlcWallMs=0, hlcCounter=0
        )

        self.assertIs(validate_record(record), record)


if __name__ == "__main__":
    unittest.main()
