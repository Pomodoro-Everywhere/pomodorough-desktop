"""Admission rule for a new task draft typed into the composer.

This is input validation in the client, not a title rule. The shared title
rules accept a title made only of spaces, and stored or replicated tasks
with such a title stay valid; only a new draft is turned away. A draft
with other characters passes unchanged, edge spaces included, because
they are part of the task identity.
"""

from __future__ import annotations

from .core import BlankTaskTitleError, normalize_task_title


def is_blank_task_draft(draft: str) -> bool:
    """Return whether the draft has nothing but whitespace after the title rules."""
    return not normalize_task_title(draft).strip()


def reject_blank_task_draft(draft: str) -> None:
    if is_blank_task_draft(draft):
        raise BlankTaskTitleError("Enter a task name that is not blank.")
