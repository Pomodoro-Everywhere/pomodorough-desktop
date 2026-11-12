"""Durable marker for a task delete that waits in the undo window.

The marker lives in the local ``meta`` table only. It is not part of a saved
workspace, a sync request or an Iroh record, so it never leaves the device.
It names the task and the workspace owner, so a later start can finish the
delete through the normal delete path, or drop the marker when it belongs to
another workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PENDING_TASK_DELETION_KEY = "pendingTaskDeletion"
_MARKER_VERSION = 1
_ACCOUNT_WORKSPACE = "account"
_LOCAL_WORKSPACE = "local"


@dataclass(frozen=True, slots=True)
class PendingTaskDeletionMarker:
    task_id: str
    # None is the signed-out local workspace.
    owner_id: str | None


def workspace_owner_id(user: Any) -> str | None:
    """Return the signed-in account id, or None for the local workspace."""
    identifier = user.get("id") if isinstance(user, dict) else None
    return identifier if isinstance(identifier, str) and identifier else None


def record_pending_task_deletion(store: Any, task_id: str, user: Any) -> None:
    owner_id = workspace_owner_id(user)
    owner = (
        {"kind": _LOCAL_WORKSPACE}
        if owner_id is None
        else {"kind": _ACCOUNT_WORKSPACE, "id": owner_id}
    )
    store.set_meta(
        PENDING_TASK_DELETION_KEY,
        {"version": _MARKER_VERSION, "taskId": task_id, "owner": owner},
    )


def clear_pending_task_deletion(store: Any) -> None:
    store.set_meta(PENDING_TASK_DELETION_KEY, None)


def stored_pending_task_deletion(store: Any) -> Any:
    """Return the stored marker value as written, or None when there is none."""
    return store.get_meta(PENDING_TASK_DELETION_KEY, None)


def unfinished_task_deletion_count(store: Any) -> int:
    """Return 1 while a recorded delete is unfinished, for pending-work counts."""
    stored = stored_pending_task_deletion(store)
    return 0 if parse_pending_task_deletion(stored) is None else 1


def parse_pending_task_deletion(stored: Any) -> PendingTaskDeletionMarker | None:
    """Return the marker, or None when the stored value is not a valid one."""
    if not isinstance(stored, dict) or stored.get("version") != _MARKER_VERSION:
        return None
    task_id = stored.get("taskId")
    owner = stored.get("owner")
    if not isinstance(task_id, str) or not task_id or not isinstance(owner, dict):
        return None
    if owner.get("kind") == _LOCAL_WORKSPACE:
        return PendingTaskDeletionMarker(task_id, None)
    owner_id = owner.get("id")
    if owner.get("kind") != _ACCOUNT_WORKSPACE or not isinstance(owner_id, str):
        return None
    if not owner_id:
        return None
    return PendingTaskDeletionMarker(task_id, owner_id)
