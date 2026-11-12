"""Reads what a controller outcome asks the window to tell the user."""

from __future__ import annotations

from collections.abc import Iterable

from pomodorough.controller_outcomes import EmitFailure, EmitNotice
from pomodorough.failure_notice import describe_failure
from pomodorough.localization import Strings


def notice_texts(effects: Iterable[object]) -> list[str]:
    """One text per notice or failure effect, in order.

    A failure effect carries the error, not a sentence: the window words it
    from the error type. It is listed here by its error text so a test can
    pin which error surfaced.
    """
    return [
        effect.message if isinstance(effect, EmitNotice) else str(effect.error)
        for effect in effects
        if isinstance(effect, (EmitNotice, EmitFailure))
    ]


def failure_sentence(error: Exception) -> str:
    """The sentence a service signal carries for ``error`` in the default locale."""
    return describe_failure(Strings(), error).sentence


def catalog_sentence(key: str, **fields: object) -> str:
    """A catalog sentence in the default locale, as a service signal carries it."""
    return Strings().text(key, **fields)
