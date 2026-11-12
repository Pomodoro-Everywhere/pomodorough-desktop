"""TimerInteractionController.rename_task mirrors core renameTask against a real Store."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pomodorough.controller_outcomes import (
    EmitTaskFailure,
    LoadState,
    Render,
    Synchronize,
)
from pomodorough.core import TaskTitleTooLongError, task_from_title
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _now() -> int:
    return int(time.time() * 1000)


def _projection(store: Store):
    state = store.load()
    proj = store.projected_state(now_ms=_now(), state=state)
    return state, proj


def _harness(store: Store):
    state, proj = _projection(store)
    tasks = list(proj.tasks)
    ctx = TimerInteractionContext(
        store=store, cloud=SimpleNamespace(authenticated=False, busy=False),
        closed=False, timer=proj.canonical_timer, settings=dict(state["settings"]),
        user=None, tasks=tasks, known_tasks={t["id"]: t for t in tasks},
        projection_now_ms=_now(), replication_mode="offline",
        history_resolution_active=False,
    )
    ports = TimerInteractionPorts(
        context=lambda: ctx, apply_outcome=Mock(), mutation_blocked=lambda: False,
        issue_command=Mock(), maybe_auto_start_break=Mock(return_value=False),
        notice=Mock(), task_input_text=Mock(return_value=""),
        clear_task_input=Mock(), task_item_data=Mock(return_value=None),
        invalidate_task_selector=Mock(), render_task_selector=Mock(),
        refresh_duration_spins=Mock(), refresh_auto_breaks=Mock(),
        stop_sound_timer=Mock(), stop_completion_sound=Mock(),
        set_stop_sound_control=Mock(),
        complete_pending_task_delete=lambda: None,
    )
    return SimpleNamespace(ctx=ctx, controller=TimerInteractionController(ports))


@pytest.fixture
def store(tmp_path):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    yield store
    store.close()


def _seed(store: Store, titles: list[str], selected: str | None) -> dict[str, dict]:
    tasks = {title: task_from_title(title) for title in titles}
    for task in tasks.values():
        store.queue_task_operation("upsert", task)
    if selected is not None:
        store.set_selected_task_id(tasks[selected]["id"])
    return tasks


def _titles(store: Store) -> set[str]:
    _, proj = _projection(store)
    return {task["title"] for task in proj.tasks}


def _selected(store: Store) -> str | None:
    state, _ = _projection(store)
    return state["settings"].get("selectedTaskId")


def _mutating(outcome) -> list[type]:
    return [type(effect) for effect in outcome.effects]


def test_rename_selected_task_moves_selection_and_removes_old(store):
    tasks = _seed(store, ["Alpha"], selected="Alpha")
    harness = _harness(store)
    assert harness.ctx.settings["selectedTaskId"] == tasks["Alpha"]["id"]

    outcome = harness.controller.rename_task(tasks["Alpha"]["id"], "Beta")

    assert _mutating(outcome) == [LoadState, Render, Synchronize]
    beta_id = task_from_title("Beta")["id"]
    assert _titles(store) == {"Beta"}
    assert _selected(store) == beta_id
    assert harness.ctx.settings["selectedTaskId"] == beta_id
    assert not store.connection.in_transaction


def test_rename_onto_existing_title_merges_and_moves_selection(store):
    tasks = _seed(store, ["Alpha", "Beta"], selected="Alpha")
    harness = _harness(store)
    pending_before = len(store.load()["pendingTasks"])

    outcome = harness.controller.rename_task(tasks["Alpha"]["id"], "Beta")

    assert _mutating(outcome) == [LoadState, Render, Synchronize]
    assert _titles(store) == {"Beta"}
    assert _selected(store) == tasks["Beta"]["id"]
    assert harness.ctx.settings["selectedTaskId"] == tasks["Beta"]["id"]
    # Merge queues only the rename of the old identity, never a second upsert.
    assert len(store.load()["pendingTasks"]) == pending_before + 1


def test_rename_unselected_task_keeps_selection(store):
    tasks = _seed(store, ["Alpha", "Gamma"], selected="Gamma")
    harness = _harness(store)
    selection_ops_before = store.load()["pendingSelectedTasks"]

    outcome = harness.controller.rename_task(tasks["Alpha"]["id"], "Beta")

    assert _mutating(outcome) == [LoadState, Render, Synchronize]
    assert _titles(store) == {"Beta", "Gamma"}
    assert _selected(store) == tasks["Gamma"]["id"]
    assert harness.ctx.settings["selectedTaskId"] == tasks["Gamma"]["id"]
    assert store.load()["pendingSelectedTasks"] == selection_ops_before


def test_rename_to_same_identity_queues_nothing(store):
    tasks = _seed(store, ["Alpha"], selected="Alpha")
    harness = _harness(store)
    before = store.load()

    outcome = harness.controller.rename_task(tasks["Alpha"]["id"], "Alpha")

    assert list(outcome.effects) == []
    after = store.load()
    assert after["pendingTasks"] == before["pendingTasks"]
    assert after["pendingSelectedTasks"] == before["pendingSelectedTasks"]
    assert after["settings"] == before["settings"]


def test_rename_unknown_task_queues_nothing(store):
    _seed(store, ["Alpha"], selected=None)
    harness = _harness(store)
    before = store.load()

    outcome = harness.controller.rename_task("missing-id", "Beta")

    assert list(outcome.effects) == []
    assert store.load()["pendingTasks"] == before["pendingTasks"]


def test_rename_to_invalid_title_reports_notice(store):
    tasks = _seed(store, ["Alpha"], selected="Alpha")
    harness = _harness(store)
    before = store.load()

    outcome = harness.controller.rename_task(tasks["Alpha"]["id"], "x" * 600)

    assert [type(effect) for effect in outcome.effects] == [EmitTaskFailure]
    assert isinstance(outcome.effects[0].error, TaskTitleTooLongError)
    assert store.load()["pendingTasks"] == before["pendingTasks"]
    assert harness.ctx.settings["selectedTaskId"] == tasks["Alpha"]["id"]
