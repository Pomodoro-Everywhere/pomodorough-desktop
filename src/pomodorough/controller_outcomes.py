from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar


@dataclass(frozen=True, slots=True)
class LoadState:
    pass


@dataclass(frozen=True, slots=True)
class Render:
    pass


@dataclass(frozen=True, slots=True)
class RenderNetwork:
    pass


@dataclass(frozen=True, slots=True)
class Synchronize:
    pass


@dataclass(frozen=True, slots=True)
class ShowWindow:
    pass


@dataclass(frozen=True, slots=True)
class SetAccountState:
    synced: bool


@dataclass(frozen=True, slots=True)
class MaybeAutoStartBreak:
    sync: bool = True
    allow_busy: bool = False
    require_canonical: bool | None = None


@dataclass(frozen=True, slots=True)
class SchedulePendingAutoBreak:
    require_canonical: bool | None = None


@dataclass(frozen=True, slots=True)
class ActivatePersistedResolution:
    pass


@dataclass(frozen=True, slots=True)
class StopSound:
    pass


@dataclass(frozen=True, slots=True)
class EmitNotice:
    message: str


@dataclass(frozen=True, slots=True)
class ShowStatus:
    message: str
    duration_ms: int = 0


ControllerEffect = (
    LoadState
    | Render
    | RenderNetwork
    | Synchronize
    | ShowWindow
    | SetAccountState
    | MaybeAutoStartBreak
    | SchedulePendingAutoBreak
    | ActivatePersistedResolution
    | StopSound
    | EmitNotice
    | ShowStatus
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ControllerOutcome(Generic[T]):
    value: T
    effects: tuple[ControllerEffect, ...] = ()


def done(*effects: ControllerEffect) -> ControllerOutcome[None]:
    return ControllerOutcome(None, effects)


def returning(value: T, *effects: ControllerEffect) -> ControllerOutcome[T]:
    return ControllerOutcome(value, effects)
