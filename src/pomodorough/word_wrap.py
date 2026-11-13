"""Greedy word wrap shared by the wrapping button widgets."""

from __future__ import annotations

from collections.abc import Callable


def wrap_words(text: str, room: int, text_width: Callable[[str], int]) -> list[str]:
    """Break text between words so each line fits room where it can.

    A word wider than the room keeps its own line; the widget then asks for
    that word's width.
    """
    lines: list[str] = []
    for word in text.split():
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and text_width(candidate) <= room:
            lines[-1] = candidate
        else:
            lines.append(word)
    return lines or [text]
