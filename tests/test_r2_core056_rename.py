"""Core 0.56.0 adoption: a rename is stored as Core's ``rename`` operation.

Before 0.56.0 Desktop stored a rename as an upsert of the new identity and a
delete of the old one, so finished history stayed on the old task ID and the
renamed task showed zero runs (R54-C04). Core 0.56.0 adds a ``rename`` task
operation: ``taskId`` is the old identity and ``title`` is the new normalized
title. Every reducer then carries finished history to the new identity.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from pomodorough.core import rebuild_tasks, task_from_title, task_summaries_today
from pomodorough.iroh_protocol import IrohProtocolError, _validate_task_operation
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from test_ux_rename_controller import MemorySecretStore, _harness

ALPHA = task_from_title("Alpha")
BETA = task_from_title("Beta")
SEVEN_MINUTES_MS = 420_000


@pytest.fixture
def store(tmp_path):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    yield store
    store.close()


def _task_operations(store: Store) -> list[tuple[str, str, str | None]]:
    return [
        (operation["type"], operation["taskId"], operation.get("title"))
        for operation in store.load()["pendingTasks"]
    ]


def _operation(**changes: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "id": "019f7f65-dd10-7000-8000-000000000002",
        "taskId": ALPHA["id"],
        "type": "rename",
        "title": BETA["title"],
        "occurredAt": "2026-03-08T07:00:00Z",
        "hlcWallMs": 1772953200000,
        "hlcCounter": 0,
    }
    operation.update(changes)
    return {key: value for key, value in operation.items() if value is not None}


def _validate_pending(operation: dict[str, Any]) -> None:
    Store._validate_pending_task_operation(
        Store.__new__(Store), operation, {"id": operation["id"]}
    )


def test_rename_queues_core_rename_of_the_old_identity(store):
    store.queue_task_operation_with_selection("upsert", ALPHA, ALPHA["id"])
    harness = _harness(store)

    harness.controller.rename_task(ALPHA["id"], "Beta")

    assert _task_operations(store) == [
        ("upsert", ALPHA["id"], "Alpha"),
        ("upsert", BETA["id"], "Beta"),
        ("rename", ALPHA["id"], "Beta"),
    ]
    projected = store.projected_state(now_ms=int(time.time() * 1000))
    assert projected.tasks == [BETA]


def test_merge_rename_queues_only_the_rename(store):
    store.queue_task_operation("upsert", ALPHA)
    store.queue_task_operation("upsert", BETA)
    harness = _harness(store)

    harness.controller.rename_task(ALPHA["id"], "Beta")

    assert _task_operations(store)[2:] == [("rename", ALPHA["id"], "Beta")]


def test_finished_focus_follows_the_renamed_task(store):
    start_ms = int(time.time() * 1000) - 600_000
    durations = store.load()["settings"]["durationsMs"]
    store.queue_task_operation_with_selection(
        "upsert", ALPHA, ALPHA["id"], now_ms=start_ms - 1_000
    )
    store.queue_command("start", None, "focus", durations, now_ms=start_ms)
    running = store.projected_state(now_ms=start_ms + 1_000).canonical_timer
    store.queue_command(
        "finish", running, "focus", durations, now_ms=start_ms + SEVEN_MINUTES_MS
    )

    _harness(store).controller.rename_task(ALPHA["id"], "Beta")

    projection = store.projected_state(now_ms=int(time.time() * 1000))
    assert [row["taskId"] for row in projection.history] == [BETA["id"]]
    totals = task_summaries_today(projection.tasks, projection.history)
    assert totals == {BETA["id"]: {"finished": 1, "timeMs": SEVEN_MINUTES_MS}}


def test_generic_queue_refuses_a_rename_of_a_valid_task(store):
    # A rename must name its new title, so only queue_task_rename builds one.
    store.queue_task_operation("upsert", ALPHA)

    with pytest.raises(ValueError, match="Unsupported task operation"):
        store.queue_task_operation("rename", ALPHA)

    assert _task_operations(store) == [("upsert", ALPHA["id"], "Alpha")]


def test_stored_rename_needs_a_normalized_title_for_another_identity():
    _validate_pending(_operation())
    for broken in (
        _operation(title=None),
        _operation(title=ALPHA["title"]),
        _operation(title=""),
        _operation(title=7),
    ):
        with pytest.raises(ValueError, match="Pending task operation is invalid"):
            _validate_pending(broken)


def test_iroh_task_record_accepts_rename_with_title_only():
    _validate_task_operation(_operation())
    with pytest.raises(IrohProtocolError):
        _validate_task_operation(_operation(title=None))


def test_python_task_rebuild_removes_the_renamed_identity():
    upsert = _operation(type="upsert", title=ALPHA["title"], hlcCounter=0)
    rename = _operation(id="019f7f65-dd10-7000-8000-000000000003", hlcCounter=1)

    assert rebuild_tasks([], [upsert, rename]) == []
