import json
import unittest

from iroh_protocol_cases import (
    DEVICE_ID,
    OPERATION_ID,
    REQUEST_ID,
    ROOM_ID,
    SECRET,
    auto_start_record,
    duration_record,
    envelope,
    genesis_record,
    task_record,
    timer_record,
)

from pomodorough.iroh_protocol import (
    INVITE_PREFIX,
    MAX_FRAME_BODY,
    MAX_INVENTORY,
    MAX_OPERATION_REFS,
    IrohProtocolError,
    b64url_encode,
    decode_frame,
    decode_message,
    encode_frame,
    encode_message,
    parse_invite,
    record_digest,
    room_id_for_secret,
    validate_message,
)


def encoded_invite(document: object) -> str:
    return INVITE_PREFIX + b64url_encode(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
            errors="surrogatepass"
        )
    )


class NegativeIntegrationTests(unittest.TestCase):
    def test_authenticated_frame_does_not_bypass_invite_room_binding(self) -> None:
        document = {
            "v": 1,
            "roomId": room_id_for_secret(bytes(reversed(range(32)))),
            "endpointTicket": "endpoint-integration-ticket",
            "roomSecret": b64url_encode(SECRET),
        }
        invite = encoded_invite(document)
        received = decode_frame(encode_frame(invite.encode(), SECRET), SECRET).decode()

        with self.assertRaisesRegex(IrohProtocolError, "does not match"):
            parse_invite(
                received,
                ticket_parser=lambda _ticket: "endpoint-integration-id",
            )

    def test_invite_transport_rejects_malformed_payload_and_field_boundaries(self) -> None:
        valid = {
            "v": 1,
            "roomId": ROOM_ID,
            "endpointTicket": "endpoint-ticket",
            "roomSecret": b64url_encode(SECRET),
        }
        documents = (
            [],
            {**valid, "v": 2},
            {**valid, "roomId": "bad"},
            {**valid, "endpointTicket": ""},
            {**valid, "endpointTicket": "\ud800"},
            {**valid, "roomName": ""},
            {**valid, "roomName": "\ud800"},
            {**valid, "roomSecret": b64url_encode(b"short")},
        )
        for document in documents:
            with self.subTest(document=document), self.assertRaises(IrohProtocolError):
                parse_invite(
                    encoded_invite(document),
                    ticket_parser=lambda _ticket: "endpoint-id",
                )

        malformed_payloads = (
            "wrong-prefix",
            INVITE_PREFIX + b64url_encode(b"\xff"),
            INVITE_PREFIX + b64url_encode(b"not-json"),
        )
        for invite in malformed_payloads:
            with self.subTest(invite=invite), self.assertRaises(IrohProtocolError):
                parse_invite(invite, ticket_parser=lambda _ticket: "endpoint-id")

        with self.assertRaisesRegex(IrohProtocolError, "no endpoint identity"):
            parse_invite(encoded_invite(valid), ticket_parser=lambda _ticket: "")

    def test_message_transport_rejects_invalid_json_duplicates_and_size(self) -> None:
        bodies = (
            b"\xff",
            b"not-json",
            b'{"kind":"hello","kind":"inventory"}',
            b"x" * (MAX_FRAME_BODY + 1),
        )
        for body in bodies:
            with self.subTest(size=len(body)), self.assertRaises(IrohProtocolError):
                decode_message(body)

    def test_message_envelope_rejects_missing_wrong_and_unknown_contracts(self) -> None:
        cases: list[object] = (
            [],
            {"kind": "hello"},
            {**envelope("hello"), "protocolVersion": True},
            {**envelope("hello"), "protocolVersion": 2},
            {**envelope("hello"), "roomId": "bad"},
            {**envelope("hello"), "requestId": "bad"},
            {**envelope("unknown")},
            {**envelope("hello"), "kind": 1},
        )
        for message in cases:
            with self.subTest(message=message), self.assertRaises(IrohProtocolError):
                validate_message(message)

    def test_hello_rejects_identity_ticket_platform_name_and_shape(self) -> None:
        base = envelope(
            "hello",
            deviceId=DEVICE_ID,
            endpointTicket="endpoint-ticket",
            platform="linux",
        )
        cases = (
            {**base, "deviceId": "short"},
            {**base, "endpointTicket": ""},
            {**base, "endpointTicket": 1},
            {**base, "platform": "beos"},
            {**base, "displayName": ""},
            {**base, "displayName": "x" * 65},
            {**base, "extra": True},
        )
        for message in cases:
            with self.subTest(message=message), self.assertRaises(IrohProtocolError):
                encode_message(message)

    def test_inventory_rejects_bad_pagination_entries_order_and_digests(self) -> None:
        invalid_requests = (
            envelope("inventory", after="bad", limit=1),
            envelope("inventory", after=None, limit=True),
            envelope("inventory", after=None, limit=0),
            envelope("inventory", after=None, limit=MAX_INVENTORY + 1),
        )
        for message in invalid_requests:
            with self.subTest(message=message), self.assertRaises(IrohProtocolError):
                validate_message(message)

        record = auto_start_record()
        entry = {
            "domain": "autoStart",
            "id": OPERATION_ID,
            "digest": record_digest(record),
        }
        bad_entries = (
            None,
            [{**entry, "digest": "bad"}],
            [{**entry, "digest": b64url_encode(b"short")}],
            [entry, entry],
            [
                {**entry, "id": "operation-z-matrix"},
                {**entry, "id": "operation-a-matrix"},
            ],
        )
        for entries in bad_entries:
            message = envelope("inventoryResult", entries=entries, next=None)
            with self.subTest(entries=entries), self.assertRaises(IrohProtocolError):
                validate_message(message)

    def test_operation_messages_reject_empty_duplicate_and_invalid_payloads(self) -> None:
        reference = {"domain": "autoStart", "id": OPERATION_ID}
        invalid_refs = (
            None,
            [],
            [reference, reference],
            [{"domain": "unknown", "id": OPERATION_ID}],
            [reference] * (MAX_OPERATION_REFS + 1),
        )
        for refs in invalid_refs:
            message = envelope("operations", refs=refs)
            with self.subTest(refs_type=type(refs).__name__), self.assertRaises(
                IrohProtocolError
            ):
                validate_message(message)

        record = auto_start_record()
        invalid_results = (
            None,
            [record, record],
            [record] * (MAX_OPERATION_REFS + 1),
        )
        for records in invalid_results:
            message = envelope("operationsResult", records=records)
            with self.subTest(records_type=type(records).__name__), self.assertRaises(
                IrohProtocolError
            ):
                validate_message(message)

    def test_error_responses_reject_unbounded_wrong_typed_and_unknown_fields(self) -> None:
        base = envelope(
            "error", code="internal", message="failure", retryable=False
        )
        cases = (
            {**base, "code": "unknown"},
            {**base, "message": 1},
            {**base, "message": "x" * 1025},
            {**base, "retryable": 0},
            {**base, "extra": True},
        )
        for message in cases:
            with self.subTest(message=message), self.assertRaises(IrohProtocolError):
                validate_message(message)

    def test_operation_records_reject_domain_specific_semantic_corruption(self) -> None:
        scenarios = (
            (timer_record(), "timerId", "short"),
            (timer_record(), "deviceSequence", 0),
            (timer_record(), "type", "unknown"),
            (timer_record(), "phase", "unknown"),
            (timer_record(), "plannedDurationMs", 59_999),
            (timer_record(), "observedElapsedMs", True),
            (timer_record(), "taskId", "short"),
            (task_record(), "taskId", "short"),
            (task_record(), "title", "Different title"),
            (duration_record(), "phase", "unknown"),
            (duration_record(), "durationMs", 59_999),
        )
        for record, key, value in scenarios:
            record["operation"][key] = value
            with self.subTest(domain=record["domain"], key=key), self.assertRaises(
                IrohProtocolError
            ):
                validate_message(envelope("operationsResult", records=[record]))

    def test_genesis_rejects_noncanonical_collections_settings_and_identities(self) -> None:
        mutations = (
            lambda operation: operation.update(history={}),
            lambda operation: operation.update(tasks={}),
            lambda operation: operation.update(tasks=[{"id": "bad", "title": "Task"}]),
            lambda operation: operation.update(
                tasks=[
                    {"id": "task-duplicate-0001", "title": "Task"},
                    {"id": "task-duplicate-0001", "title": "Task"},
                ]
            ),
            lambda operation: operation.update(selectedTaskId="short"),
            lambda operation: operation.update(autoStartBreaks=1),
            lambda operation: operation.update(hlcWallMs=0, hlcCounter=1),
            lambda operation: operation.update(canonicalTimer={}),
            lambda operation: operation.update(extra=True),
        )
        for mutation in mutations:
            record = genesis_record()
            mutation(record["operation"])
            with self.subTest(operation=record["operation"]), self.assertRaises(
                IrohProtocolError
            ):
                validate_message(envelope("operationsResult", records=[record]))

    def test_authenticated_invalid_message_is_rejected_after_frame_acceptance(self) -> None:
        invalid = envelope("operations", refs=[])
        body = json.dumps(invalid, separators=(",", ":")).encode()
        authenticated = decode_frame(encode_frame(body, SECRET), SECRET)

        with self.assertRaisesRegex(IrohProtocolError, "references"):
            decode_message(authenticated)
        self.assertEqual(invalid["requestId"], REQUEST_ID)


if __name__ == "__main__":
    unittest.main()
