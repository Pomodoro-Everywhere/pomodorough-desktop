from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar


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


class TaskChange(Enum):
    ADD = "add"
    DELETE = "delete"
    RENAME = "rename"


@dataclass(frozen=True, slots=True)
class EmitTaskFailure:
    """A task change failed. The window words it; the error text stays internal."""

    change: TaskChange
    error: Exception
    retry: Callable[[], ControllerOutcome[None]]


@dataclass(frozen=True, slots=True)
class EmitFailure:
    """An action failed. The window words it from the error type.

    The error text stays internal. ``retry`` runs the same action again;
    leave it out where the user has nothing to repeat.
    """

    error: Exception
    retry: Callable[[], ControllerOutcome[Any]] | None = None


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
    | EmitTaskFailure
    | EmitFailure
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
