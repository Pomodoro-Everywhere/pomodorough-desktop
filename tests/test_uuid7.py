from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from pathlib import Path

from pomodorough.uuid7 import (
    UUID7_MAX_TIMESTAMP_MS,
    UUID7_RANDOM_MAX,
    reserve_uuid7,
    uuid7_from_parts,
    uuid7_parts,
)


class UUID7Tests(unittest.TestCase):
    def test_rfc_9562_vector(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "uuidv7-v1.json"
        fixture_bytes = fixture_path.read_bytes()
        self.assertEqual(
            hashlib.sha256(fixture_bytes).hexdigest(),
            "719bf4601f0e82aa9898e891184edcf8f37b183a05f3ddd6fa211e1ac8dc2f10",
        )
        fixture = json.loads(fixture_bytes)["rfc9562"]
        timestamp_ms = fixture["timestampMs"]
        random_value = int(fixture["randomValueHex"], 16)

        generated = uuid7_from_parts(timestamp_ms, random_value)

        self.assertEqual(generated, fixture["uuid"])
        self.assertEqual(uuid7_parts(generated), (timestamp_ms, random_value))
        parsed = uuid.UUID(generated)
        self.assertEqual(parsed.version, 7)
        self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_reservation_is_monotonic_within_one_millisecond(self) -> None:
        entropy = lambda count: bytes.fromhex("00000000000000000009")

        first = reserve_uuid7(1_000, 3, None, entropy=entropy)
        second = reserve_uuid7(1_000, 2, first[-1], entropy=entropy)

        self.assertEqual(
            [uuid7_parts(value)[1] for value in first + second],
            [9, 10, 11, 12, 13],
        )
        self.assertEqual(first + second, sorted(first + second))

    def test_new_timestamp_reseeds_random_value(self) -> None:
        first = uuid7_from_parts(1_000, 99)

        generated = reserve_uuid7(
            1_001,
            1,
            first,
            entropy=lambda count: bytes.fromhex("00000000000000000007"),
        )

        self.assertEqual(uuid7_parts(generated[0]), (1_001, 7))
        self.assertGreater(generated[0], first)

    def test_timestamp_rollback_reuses_previous_timestamp(self) -> None:
        previous = uuid7_from_parts(1_000, 9)

        generated = reserve_uuid7(999, 2, previous)

        self.assertEqual(
            [uuid7_parts(value) for value in generated],
            [(1_000, 10), (1_000, 11)],
        )

    def test_tail_overflow_fails(self) -> None:
        previous = uuid7_from_parts(1_000, UUID7_RANDOM_MAX)

        with self.assertRaisesRegex(ValueError, "no headroom"):
            reserve_uuid7(1_000, 1, previous)

    def test_timestamp_and_entropy_bounds_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "timestamp"):
            reserve_uuid7(0, 1, None)
        with self.assertRaisesRegex(ValueError, "timestamp"):
            reserve_uuid7(UUID7_MAX_TIMESTAMP_MS + 1, 1, None)
        with self.assertRaisesRegex(ValueError, "entropy"):
            reserve_uuid7(1, 1, None, entropy=lambda count: b"short")
        with self.assertRaisesRegex(ValueError, "version or variant"):
            uuid7_parts(str(uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
