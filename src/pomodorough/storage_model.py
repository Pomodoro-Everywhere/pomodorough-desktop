"""Shared storage values and value-level helpers.

This module has no database dependency. Storage and synchronization adapters import it
without creating a cycle between their mixins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from .shared_core import SharedCore

DURATION_MIN_MS = 60_000
PREFERENCE_DURATION_MAX_MS = 10_800_000
CANONICAL_DURATION_MAX_MS = 14_400_000
RESOLUTION_OPERATION_MAX = 4_096
ACKNOWLEDGEMENT_OUTCOMES = {"applied", "ignored", "rejected"}
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CLOCK_SKEW_MS = 300_000
MAX_SERVER_TIME_UNCERTAINTY_MS = 30_000
MAX_CLOCK_CONTINUITY_DRIFT_MS = 1_000
COMMAND_TYPES = {"start", "pause", "resume", "finish", "cancel", "clear"}


_DEFAULT_SHARED_CORE: SharedCore | None = None
_DEFAULT_SHARED_CORE_LOCK = Lock()


def _default_shared_core() -> SharedCore:
    global _DEFAULT_SHARED_CORE
    with _DEFAULT_SHARED_CORE_LOCK:
        if _DEFAULT_SHARED_CORE is None:
            _DEFAULT_SHARED_CORE = SharedCore()
        return _DEFAULT_SHARED_CORE


def utc_timestamp(milliseconds: int) -> str:
    try:
        value = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("timestamp is out of range") from error
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
