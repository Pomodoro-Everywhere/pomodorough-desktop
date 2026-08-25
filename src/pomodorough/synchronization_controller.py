from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QTimer

from .controller_outcomes import (
    ControllerOutcome,
    EmitNotice,
    LoadState,
    MaybeAutoStartBreak,
    Render,
    SchedulePendingAutoBreak,
    SetAccountState,
    ShowStatus,
    Synchronize,
    done,
)


@dataclass(frozen=True, slots=True)
class SynchronizationContext:
    store: Any
    cloud: Any
    iroh: Any | None
    strings: Any
    closed: bool
    revision: int
    replication_mode: str
    iroh_join_pending: bool
    history_resolution_active: bool


@dataclass(frozen=True, slots=True)
class SynchronizationPorts:
    context: Callable[[], SynchronizationContext]
    apply_outcome: Callable[[ControllerOutcome[Any]], None]
    response_timing: Callable[[dict[str, Any]], dict[str, int | None]]
    activate_persisted_resolution: Callable[[], bool]
    continue_history_resolution: Callable[[], None]
    retry_sync: Callable[[], None]
    synchronize: Callable[[], None]
    iroh_failure: Callable[[str], None]


class SynchronizationController:
    """Owns centralized request/retry state and sync response application."""

    def __init__(self, ports: SynchronizationPorts) -> None:
        self._ports = ports
        self.sync_request: dict[str, Any] | None = None
        self.sync_waiting = False

    def _context(self) -> SynchronizationContext:
        return self._ports.context()

    def clear_request(self) -> None:
        self.sync_request = None

    def sync(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.closed or context.iroh_join_pending:
            return done()
        if context.replication_mode == "iroh":
            return self._sync_iroh(context)
        if context.replication_mode != "centralized":
            return done()
        return self._sync_centralized(context)

    def _sync_iroh(self, context: SynchronizationContext) -> ControllerOutcome[None]:
        try:
            changed = context.store.capture_local_iroh_records()
        except (OSError, ValueError) as error:
            self._ports.iroh_failure(str(error))
            return done()
        if changed:
            self._ports.apply_outcome(done(LoadState(), Render()))
        if context.iroh is not None:
            context.iroh.sync_now()
        return done()

    def _sync_centralized(
        self, context: SynchronizationContext
    ) -> ControllerOutcome[None]:
        if (
            not context.history_resolution_active
            and context.store.pending_resolution() is not None
        ):
            self._ports.activate_persisted_resolution()
            self._ports.apply_outcome(done(Render(), SetAccountState(False)))
            context = self._context()
        if not context.cloud.authenticated:
            return done()
        if context.history_resolution_active:
            self._ports.continue_history_resolution()
            return done()
        payload = context.store.sync_payload()
        has_pending = bool(
            payload["commands"]
            or payload["taskOperations"]
            or payload["durationOperations"]
            or payload["autoStartOperations"]
            or payload["selectedTaskOperations"]
        )
        if has_pending:
            self._ports.apply_outcome(done(SetAccountState(False)))
        if context.cloud.busy:
            return self.sync_when_available()
        self.sync_request = payload
        context.cloud.sync(payload)
        return done()

    def sync_when_available(self) -> ControllerOutcome[None]:
        if self.sync_waiting:
            return done()
        self.sync_waiting = True
        QTimer.singleShot(100, self._ports.retry_sync)
        return done()

    def retry_sync(self) -> ControllerOutcome[None]:
        if self._context().cloud.busy:
            QTimer.singleShot(100, self._ports.retry_sync)
            return done()
        self.sync_waiting = False
        self._ports.synchronize()
        return done()

    def remote_revision_available(self, revision: int) -> ControllerOutcome[None]:
        if revision > self._context().revision:
            self._ports.synchronize()
        return done()

    def apply_sync(self, response: dict[str, Any]) -> ControllerOutcome[None]:
        context = self._context()
        request = self.sync_request
        self.sync_request = None
        if request is None:
            return done(
                SetAccountState(False),
                EmitNotice(context.strings.text("resolution.stale_response")),
            )
        try:
            notices = context.store.apply_sync(
                response,
                request,
                **self._ports.response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self._ports.apply_outcome(self.cloud_failure(str(error)))
            return done(EmitNotice(str(error)))
        self._ports.apply_outcome(
            done(
                LoadState(),
                Render(),
                MaybeAutoStartBreak(sync=False, allow_busy=True),
            )
        )
        context = self._context()
        has_pending = context.store.has_sendable_sync_operations()
        effects: list[Any] = [SetAccountState(not has_pending)]
        if has_pending:
            effects.append(Synchronize())
        if notices:
            effects.append(
                EmitNotice(
                    context.strings.text(
                        "resolution.sync_conflict", detail="; ".join(notices)
                    )
                )
            )
        return done(*effects)

    def cloud_failure(self, message: str) -> ControllerOutcome[None]:
        self.sync_request = None
        context = self._context()
        effects: list[Any] = []
        if context.cloud.authenticated:
            effects.append(SetAccountState(False))
        effects.append(SchedulePendingAutoBreak(require_canonical=False))
        if "Sign in to sync" not in message:
            effects.append(ShowStatus(message, 10_000))
        return done(*effects)
