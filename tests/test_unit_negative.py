import builtins
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from iroh_protocol_cases import SECRET, TIMESTAMP_MS, auto_start_record, changed

from pomodorough.iroh_protocol import (
    MAX_FRAME_BODY,
    IrohProtocolError,
    b64url_decode,
    canonical_json,
    create_invite,
    decode_frame,
    encode_frame,
    room_id_for_secret,
    valid_identifier,
    valid_request_id,
    valid_room_id,
    validate_record,
)
from pomodorough.storage import MAX_SAFE_INTEGER
from pomodorough.uuid7 import uuid7_from_parts


class NegativeUnitTests(unittest.TestCase):
    def test_canonical_json_rejects_nonportable_values(self) -> None:
        for value in (
            1.5,
            MAX_SAFE_INTEGER + 1,
            {1: "non-string key"},
            {"bad": "\ud800"},
        ):
            with self.subTest(value=value), self.assertRaises(IrohProtocolError):
                canonical_json(value)

    def test_base64url_rejects_noncanonical_and_malformed_inputs(self) -> None:
        for value in (None, "", "a", "a=", "+w", "%%%%", "é"):
            with self.subTest(value=value), self.assertRaisesRegex(
                IrohProtocolError, "Malformed"
            ):
                b64url_decode(value)  # type: ignore[arg-type]

    def test_identity_validators_reject_wrong_types_shapes_and_versions(self) -> None:
        invalid_identifiers = (None, "short", " startsbad", "bad/slash", "x" * 129)
        for value in invalid_identifiers:
            with self.subTest(kind="identifier", value=value):
                self.assertFalse(valid_identifier(value))

        for value in (None, "not-base64", "YQ", "A" * 42):
            with self.subTest(kind="room", value=value):
                self.assertFalse(valid_room_id(value))
        for value in (None, "not-a-uuid", uuid7_from_parts(TIMESTAMP_MS, 1)[:-1]):
            with self.subTest(kind="request", value=value):
                self.assertFalse(valid_request_id(value))

    def test_frame_boundaries_reject_wrong_types_secrets_and_lengths(self) -> None:
        secret = SECRET
        with self.assertRaisesRegex(IrohProtocolError, "invalid"):
            encode_frame("body", secret)  # type: ignore[arg-type]
        with self.assertRaisesRegex(IrohProtocolError, "invalid"):
            encode_frame(b"body", secret[:-1])
        with self.assertRaisesRegex(IrohProtocolError, "exceeds"):
            encode_frame(b"x" * (MAX_FRAME_BODY + 1), secret)

        for frame, key, message in (
            (b"short", secret, "malformed"),
            (b"x" * 36, secret[:-1], "malformed"),
            ((99).to_bytes(4, "big") + b"x" * 32, secret, "length"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                IrohProtocolError, message
            ):
                decode_frame(frame, key)

    def test_room_secret_requires_exact_bytes(self) -> None:
        for value in (b"", b"x" * 31, b"x" * 33, bytearray(32)):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(
                IrohProtocolError, "exactly 32"
            ):
                room_id_for_secret(value)  # type: ignore[arg-type]

    def test_record_wrapper_fails_closed_before_domain_validation(self) -> None:
        base = auto_start_record()
        cases = []
        for mutation in (
            lambda value: value.update(extra=True),
            lambda value: value.pop("deviceId"),
            lambda value: value.update(domain="unknown"),
            lambda value: value.update(deviceId="short"),
            lambda value: value.update(operation=[]),
        ):
            value = changed(base)
            mutation(value)
            cases.append(value)
        cases.append([])

        for value in cases:
            with self.subTest(value=value), self.assertRaises(IrohProtocolError):
                validate_record(value)

    def test_invite_creation_rejects_ticket_name_and_identity_boundaries(self) -> None:
        cases = (
            ("", None, "endpoint-id"),
            ("\ud800", None, "endpoint-id"),
            ("ticket", "", "endpoint-id"),
            ("ticket", "\ud800", "endpoint-id"),
            ("ticket", None, ""),
        )
        for ticket, name, endpoint_id in cases:
            with self.subTest(ticket=ticket, name=name), self.assertRaises(
                IrohProtocolError
            ):
                create_invite(
                    SECRET,
                    ticket,
                    name,
                    ticket_parser=lambda _ticket, value=endpoint_id: value,
                )

    def test_default_ticket_parser_classifies_missing_and_malformed_native_runtime(
        self,
    ) -> None:
        real_import = builtins.__import__

        def unavailable(name: str, *args: object, **kwargs: object) -> object:
            if name == "iroh":
                raise ImportError("missing native runtime")
            return real_import(name, *args, **kwargs)

        with (
            patch.object(builtins, "__import__", side_effect=unavailable),
            self.assertRaisesRegex(IrohProtocolError, "unavailable"),
        ):
            create_invite(SECRET, "endpoint-ticket")

        native = SimpleNamespace(
            EndpointTicket=SimpleNamespace(
                from_string=lambda _value: (_ for _ in ()).throw(ValueError("bad"))
            )
        )
        with (
            patch.dict(sys.modules, {"iroh": native}),
            self.assertRaisesRegex(IrohProtocolError, "malformed"),
        ):
            create_invite(SECRET, "endpoint-ticket")

    def test_auto_start_record_rejects_invalid_clock_enabled_and_shape(self) -> None:
        base = auto_start_record()
        mutations = (
            lambda operation: operation.update(enabled=1),
            lambda operation: operation.update(hlcWallMs=True),
            lambda operation: operation.update(hlcWallMs=-1),
            lambda operation: operation.update(hlcCounter=MAX_SAFE_INTEGER + 1),
            lambda operation: operation.update(occurredAt="not-a-timestamp"),
            lambda operation: operation.update(hlcWallMs=0, hlcCounter=1),
            lambda operation: operation.update(
                occurredAt="1970-01-01T00:00:00.000Z", hlcWallMs=0, hlcCounter=1
            ),
            lambda operation: operation.update(extra=True),
        )
        for mutation in mutations:
            value = changed(base)
            mutation(value["operation"])
            with self.subTest(operation=value["operation"]), self.assertRaises(
                IrohProtocolError
            ):
                validate_record(value)


if __name__ == "__main__":
    unittest.main()
