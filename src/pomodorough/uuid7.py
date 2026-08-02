from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable

UUID7_MAX_TIMESTAMP_MS = (1 << 48) - 1
UUID7_RANDOM_BITS = 74
UUID7_RANDOM_MAX = (1 << UUID7_RANDOM_BITS) - 1
_UUID7_RAND_B_MASK = (1 << 62) - 1
_ENTROPY_ATTEMPTS = 16


def uuid7_from_parts(timestamp_ms: int, random_value: int) -> str:
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or not 0 <= timestamp_ms <= UUID7_MAX_TIMESTAMP_MS
    ):
        raise ValueError("UUIDv7 timestamp is outside the 48-bit range.")
    if (
        isinstance(random_value, bool)
        or not isinstance(random_value, int)
        or not 0 <= random_value <= UUID7_RANDOM_MAX
    ):
        raise ValueError("UUIDv7 random value is outside the 74-bit range.")

    rand_a = random_value >> 62
    rand_b = random_value & _UUID7_RAND_B_MASK
    value = (timestamp_ms << 80) | (7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


def uuid7_parts(value: str) -> tuple[int, int]:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Persisted UUIDv7 state is invalid.") from error
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ValueError("Persisted UUIDv7 state has invalid version or variant.")

    integer = parsed.int
    timestamp_ms = integer >> 80
    rand_a = (integer >> 64) & 0xFFF
    rand_b = integer & _UUID7_RAND_B_MASK
    return timestamp_ms, (rand_a << 62) | rand_b


def reserve_uuid7(
    timestamp_ms: int,
    count: int,
    previous: str | None,
    *,
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> list[str]:
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or not 0 < timestamp_ms <= UUID7_MAX_TIMESTAMP_MS
    ):
        raise ValueError("UUIDv7 timestamp is outside the supported range.")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("UUIDv7 reservation count must be positive.")
    if count > UUID7_RANDOM_MAX + 1:
        raise ValueError("UUIDv7 reservation has no random-value headroom.")

    previous_parts = uuid7_parts(previous) if previous is not None else None
    if previous_parts is not None:
        previous_timestamp, previous_random = previous_parts
        if timestamp_ms <= previous_timestamp:
            if previous_random > UUID7_RANDOM_MAX - count:
                raise ValueError("UUIDv7 random value has no headroom.")
            first_random = previous_random + 1
            return [
                uuid7_from_parts(previous_timestamp, first_random + offset)
                for offset in range(count)
            ]

    maximum_first = UUID7_RANDOM_MAX - (count - 1)
    for _ in range(_ENTROPY_ATTEMPTS):
        random_bytes = entropy(10)
        if not isinstance(random_bytes, bytes) or len(random_bytes) != 10:
            raise ValueError("UUIDv7 entropy source returned invalid data.")
        first_random = int.from_bytes(random_bytes, "big") & UUID7_RANDOM_MAX
        if first_random <= maximum_first:
            return [
                uuid7_from_parts(timestamp_ms, first_random + offset)
                for offset in range(count)
            ]
    raise ValueError("UUIDv7 entropy lacks reservation headroom.")
