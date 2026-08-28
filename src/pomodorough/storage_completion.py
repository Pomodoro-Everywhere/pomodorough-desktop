from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .core import parse_timestamp_ms
from .shared_core import (
    ProjectionApplyV2,
    SharedCoreDispatcher,
    SharedCoreError,
    TimerCompletionPlanV1,
    plan_timer_completion_v1,
)
from .storage_canonical_reconciliation import generated_break_day_bounds
from .storage_model import _default_shared_core


class TimerCompletionPolicy:
    """Own completion-plan input construction and typed SharedCore dispatch."""

    def __init__(
        self,
        shared_core: Callable[[], SharedCoreDispatcher | None],
        device_id: Callable[[], str],
        replication_mode: Callable[[], str],
        read_meta: Callable[[str, Any], Any],
    ) -> None:
        self._shared_core = shared_core
        self._device_id = device_id
        self._replication_mode = replication_mode
        self._read_meta = read_meta

    def command_request(
        self,
        command_type: str,
        requested_timer: dict[str, Any] | None,
        projected_timer: dict[str, Any] | None,
        automatic: bool,
        generate_auto_break: bool,
        auto_start_breaks: bool,
    ) -> TimerCompletionPlanV1:
        ownership_timer = projected_timer if automatic else requested_timer
        return self._plan({
            "kind": "commandRequest",
            "commandType": command_type,
            "requestedTimer": requested_timer,
            "projectedTimer": projected_timer,
            "automatic": automatic,
            "generateAutoBreak": generate_auto_break,
            "autoStartBreaks": auto_start_breaks,
            "localDeviceId": self._device_id(),
            "ownership": self._ownership(ownership_timer),
        })

    def finish_applied(
        self,
        command: dict[str, Any],
        timer: dict[str, Any] | None,
        projection: ProjectionApplyV2,
    ) -> TimerCompletionPlanV1:
        day_start, day_end = self._bounds(command["occurredAt"])
        return self._plan({
            "kind": "finishApplied",
            "source": {
                "commandId": command["id"],
                "timerId": command["timerId"],
                "phase": command["phase"],
                "occurredAt": command["occurredAt"],
            },
            "history": projection.history,
            "autoStartBreaks": projection.auto_start_breaks,
            "localDeviceId": self._device_id(),
            "ownership": self._ownership(timer),
            "dayStart": day_start,
            "dayEnd": day_end,
        })

    def generated_break(
        self,
        source: dict[str, str],
        canonical: dict[str, Any],
        optimistic: ProjectionApplyV2,
        source_pending: bool,
        require_canonical: bool,
        source_timestamp: str,
    ) -> TimerCompletionPlanV1:
        day_start, day_end = self._bounds(source_timestamp)
        return self._plan({
            "kind": "generatedBreak",
            "source": source,
            "canonical": self._projection(
                canonical.get("canonicalTimer"), canonical.get("history", [])
            ),
            "optimistic": self._projection(
                optimistic.canonical_timer, optimistic.history
            ),
            "sourceFinishPending": source_pending,
            "requireCanonical": require_canonical,
            "dayStart": day_start,
            "dayEnd": day_end,
        })

    def _plan(self, input_value: object) -> TimerCompletionPlanV1:
        core = self._shared_core() or _default_shared_core()
        try:
            return plan_timer_completion_v1(core, input_value)
        except SharedCoreError as error:
            raise ValueError(str(error)) from error

    def _ownership(self, timer: Any) -> dict[str, str] | None:
        if self._replication_mode() == "iroh":
            timer_id = timer.get("id") if isinstance(timer, dict) else None
            owner = timer.get("startedByDeviceId") if isinstance(timer, dict) else None
        else:
            ownership = self._read_meta("centralizedTimerOwnership", None)
            timer_id = ownership.get("timerId") if isinstance(ownership, dict) else None
            owner = ownership.get("deviceId") if isinstance(ownership, dict) else None
        if not isinstance(timer_id, str) or not isinstance(owner, str):
            return None
        if not timer_id or not owner:
            return None
        return {"timerId": timer_id, "ownerDeviceId": owner}

    @staticmethod
    def _bounds(timestamp: str) -> tuple[str, str]:
        return generated_break_day_bounds(parse_timestamp_ms(timestamp))

    @staticmethod
    def _projection(timer: Any, history: Any) -> dict[str, Any]:
        return {"canonicalTimer": timer, "history": history}
