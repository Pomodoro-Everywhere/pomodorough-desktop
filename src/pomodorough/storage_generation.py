from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .shared_core import SharedCoreDispatcher, SharedCoreError
from .storage_model import MAX_CLOCK_SKEW_MS, MAX_SAFE_INTEGER, _default_shared_core


class GenerationReservation:
    """Own sequence allocation and SharedCore-backed HLC batch policy."""

    def __init__(
        self,
        read_meta: Callable[[str, Any], Any],
        trusted_now: Callable[..., int],
        shared_core: Callable[[], SharedCoreDispatcher | None],
    ) -> None:
        self._read_meta = read_meta
        self._trusted_now = trusted_now
        self._shared_core = shared_core

    def reserve(
        self,
        physical_now_ms: int,
        *,
        sequence_count: int = 0,
        clock_count: int = 1,
        use_server_clock: bool = True,
        use_monotonic: bool = False,
    ) -> tuple[int, list[int], list[tuple[int, int]]]:
        self._validate_counts(sequence_count, clock_count)
        now_ms = self._trusted_now(
            physical_now_ms,
            use_server_clock=use_server_clock,
            use_monotonic=use_monotonic,
        )
        sequence = self._safe_integer(
            self._read_meta("deviceSequence", 0), "Persisted device sequence"
        )
        local = self._clock(self._read_meta("hlc", {"wallMs": 0, "counter": 0}))
        if sequence_count > MAX_SAFE_INTEGER - sequence:
            raise ValueError("Device sequence has no safe integer headroom.")
        if local[0] - now_ms > MAX_CLOCK_SKEW_MS:
            raise ValueError("Persisted logical clock exceeds the trusted-time limit.")
        sequences = [sequence + offset for offset in range(1, sequence_count + 1)]
        clocks = self._tick_batch(local, now_ms, clock_count)
        return now_ms, sequences, clocks

    def _tick_batch(
        self, local: tuple[int, int], now_ms: int, count: int
    ) -> list[tuple[int, int]]:
        core = self._shared_core() or _default_shared_core()
        clocks: list[tuple[int, int]] = []
        for _index in range(count):
            try:
                output = core.dispatch("hlc.tick.v1", {
                    "local": {"wallMs": local[0], "counter": local[1]},
                    "physicalNowMs": now_ms,
                })
            except SharedCoreError as error:
                if local[0] == now_ms and local[1] == MAX_SAFE_INTEGER:
                    raise ValueError(
                        "Logical clock counter has no safe integer headroom."
                    ) from error
                raise ValueError(str(error)) from error
            local = self._validated_tick(output)
            clocks.append(local)
        return clocks

    @classmethod
    def _validated_tick(cls, value: object) -> tuple[int, int]:
        if not isinstance(value, dict) or set(value) != {"wallMs", "counter"}:
            raise ValueError("SharedCore returned malformed hlc.tick.v1 output.")
        wall_ms = cls._safe_integer(value["wallMs"], "SharedCore HLC wall time")
        counter = cls._safe_integer(value["counter"], "SharedCore HLC counter")
        return wall_ms, counter

    @staticmethod
    def _validate_counts(sequence_count: int, clock_count: int) -> None:
        if (
            isinstance(sequence_count, bool)
            or isinstance(clock_count, bool)
            or sequence_count < 0
            or clock_count < 0
        ):
            raise ValueError("Generation reservation is invalid.")

    @staticmethod
    def _safe_integer(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} is invalid.")
        if not 0 <= value <= MAX_SAFE_INTEGER:
            raise ValueError(f"{label} exceeds the supported range.")
        return value

    @classmethod
    def _clock(cls, value: object) -> tuple[int, int]:
        if not isinstance(value, dict):
            raise ValueError("Persisted logical clock is invalid.")
        wall_ms = cls._safe_integer(value.get("wallMs"), "Logical clock wall time")
        counter = cls._safe_integer(value.get("counter"), "Logical clock counter")
        if wall_ms == 0 and counter != 0:
            raise ValueError("Persisted logical clock is invalid.")
        return wall_ms, counter
