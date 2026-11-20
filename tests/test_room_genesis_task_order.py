"""A new Iroh room keeps the shared Core task list order of its genesis."""

from __future__ import annotations

import pytest

from pomodorough.core import task_from_title
from pomodorough.storage import Store
from test_storage_replication_modules import MemorySecretStore


ROOM_SECRET = bytes(range(32))
# Natural, case-folded order. A bytewise sort gives neither this order nor
# its reverse, so a local re-sort cannot pass both cases.
NATURAL_ORDER = ["a", "B", "Bulk task 2", "Bulk task 10"]


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state.sqlite3", iroh_secret_store=MemorySecretStore())
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("titles", [NATURAL_ORDER, NATURAL_ORDER[::-1]])
def test_room_genesis_snapshot_keeps_the_given_task_list_order(store, titles):
    projection = store._replication_storage._projection
    tasks = [task_from_title(title) for title in titles]
    genesis = {**projection.projected_local_genesis(), "tasks": tasks}

    snapshot = projection.empty_workspace(genesis)["metadata"]["snapshot"]

    assert snapshot["tasks"] == tasks
    assert snapshot["knownTasks"] == tasks


def test_created_room_snapshot_lists_tasks_in_the_bundled_core_order(store):
    for title in ("Bulk task 10", "a", "Bulk task 2", "B"):
        store.queue_task_operation("upsert", task_from_title(title))
    projection = store._replication_storage._projection
    core_tasks = projection.projected_local_genesis()["tasks"]

    store.create_iroh_room(ROOM_SECRET)

    assert sorted(task["title"] for task in core_tasks) == sorted(
        ["Bulk task 10", "a", "Bulk task 2", "B"]
    )
    assert store.load()["snapshot"]["tasks"] == core_tasks
