"""R48-D02: task Add/selected Delete must commit atomically with selection."""

from __future__ import annotations

import copy
import os
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from pomodorough.controller_outcomes import EmitNotice, LoadState, Render, Synchronize
from pomodorough.core import task_from_title
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
    import time as _t

    return int(_t.time() * 1000)


def _harness(store: Store, title_override: str | None = None):
    state = store.load()
    now = _now()
    try:
        proj = store.projected_state(now_ms=now, state=state)
        timer = proj.canonical_timer
        tasks = list(proj.tasks)
    except Exception:
        timer, tasks = None, []
    known = {t["id"]: t for t in tasks}
    settings = dict(state["settings"])
    ctx = TimerInteractionContext(
        store=store, cloud=SimpleNamespace(authenticated=False, busy=False),
        closed=False, timer=timer, settings=settings, user=None,
        tasks=tasks, known_tasks=known, projection_now_ms=now,
        replication_mode="offline", history_resolution_active=False,
    )
    ports = TimerInteractionPorts(
        context=lambda: ctx, apply_outcome=Mock(), mutation_blocked=lambda: False,
        issue_command=Mock(), maybe_auto_start_break=Mock(return_value=False),
        notice=Mock(), task_input_text=Mock(return_value=title_override or "R48 D02 task"),
        clear_task_input=Mock(), task_item_data=Mock(return_value=None),
        invalidate_task_selector=Mock(), render_task_selector=Mock(),
        refresh_duration_spins=Mock(), refresh_auto_breaks=Mock(),
        stop_sound_timer=Mock(), stop_completion_sound=Mock(),
        set_stop_sound_control=Mock(),
    )
    return SimpleNamespace(store=store, ctx=ctx, ports=ports, controller=TimerInteractionController(ports))


def _seed(store: Store, focus: str) -> dict:
    if focus == "active":
        settings = store.load()["settings"]
        store.queue_command("start", None, "focus", settings["durationsMs"], now_ms=_now())
    return store.load()


@pytest.fixture(params=("idle", "active"))
def task_window(tmp_path, request):
    path = tmp_path / "state.sqlite3"
    store = Store(path, shared_core=SharedCore(), iroh_secret_store=MemorySecretStore())
    _seed(store, request.param)
    yield SimpleNamespace(store=store, path=path, focus=request.param, claim=None)
    store.close()


def _snapshot(store: Store) -> dict:
    state = store.load()
    return {
        "rows": tuple(store.connection.iterdump()),
        "tasks": copy.deepcopy(state["pendingTasks"]),
        "selected": copy.deepcopy(state["pendingSelectedTasks"]),
        "commands": copy.deepcopy(state["pending"]),
        "settings": copy.deepcopy(state["settings"]),
        "proof": copy.deepcopy(store.delivery_proof()),
        "claim_meta": copy.deepcopy(store.get_meta("pendingSync")),
        "never": copy.deepcopy(store.never_sent_claim({})),
        "hlc": copy.deepcopy(store.get_meta("hlc")),
        "seq": store.get_meta("deviceSequence"),
        "uuid": store.get_meta("lastUuidV7"),
    }


def _assert_snapshot_equal(store: Store, before: dict) -> None:
    after = _snapshot(store)
    assert after == before


@contextmanager
def _fail_boundary(store: Store, boundary: str):
    conn = store.connection
    txns: list[str] = []
    seen_selection: list[bool] = []
    if boundary == "second_write":
        conn.execute(
            "CREATE TEMP TRIGGER r48d02_fail BEFORE INSERT ON "
            "pending_selected_task_operations BEGIN "
            "SELECT RAISE(ABORT, 'R48-D02 second-write rejected'); END"
        )

    def _trace(sql: str) -> None:
        if "INSERT INTO pending_selected_task_operations" in sql:
            seen_selection.append(True)

    def authorize(action, op, _a, _d, _t):
        if action == sqlite3.SQLITE_TRANSACTION:
            txns.append(op)
            # Deny the commit that carries the selection write, so the
            # first task-only commit still succeeds on split implementations
            # while the combined commit rolls back atomically.
            if boundary == "commit" and op == "COMMIT" and seen_selection:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if boundary == "commit":
        conn.set_trace_callback(_trace)
        conn.set_authorizer(authorize)
    try:
        yield txns
    finally:
        if boundary == "commit":
            conn.set_authorizer(None)
            conn.set_trace_callback(None)
        else:
            conn.execute("DROP TRIGGER r48d02_fail")


def _fail_add(harness, boundary: str, title: str):
    store = harness.store
    before = _snapshot(store)
    mem_before = dict(harness.ctx.settings)
    with _fail_boundary(store, boundary), patch(
        "pomodorough.timer_interaction_controller.capture_exception"
    ) as cap:
        outcome = harness.controller.add_task(title)
    cap.assert_called_once()
    assert isinstance(cap.call_args.args[0], sqlite3.Error)
    assert [e.message for e in outcome.effects if isinstance(e, EmitNotice)] == [
        str(cap.call_args.args[0])
    ]
    assert not any(isinstance(e, (LoadState, Render, Synchronize)) for e in outcome.effects)
    assert not store.connection.in_transaction
    _assert_snapshot_equal(store, before)
    assert harness.ctx.settings == mem_before
    return before


def _fail_delete_selected(harness, boundary: str, task_id: str):
    store = harness.store
    before = _snapshot(store)
    mem_before = dict(harness.ctx.settings)
    with _fail_boundary(store, boundary), patch(
        "pomodorough.timer_interaction_controller.capture_exception"
    ) as cap:
        outcome = harness.controller.delete_task(task_id)
    cap.assert_called_once()
    assert isinstance(cap.call_args.args[0], sqlite3.Error)
    assert [e.message for e in outcome.effects if isinstance(e, EmitNotice)] == [
        str(cap.call_args.args[0])
    ]
    assert not any(isinstance(e, (LoadState, Render, Synchronize)) for e in outcome.effects)
    assert not store.connection.in_transaction
    _assert_snapshot_equal(store, before)
    assert harness.ctx.settings == mem_before
    return before


def _reopen_equals(path, before: dict) -> None:
    with closing(Store(path, shared_core=SharedCore(), iroh_secret_store=MemorySecretStore())) as ro:
        assert _snapshot(ro)["rows"] == before["rows"]
        assert ro.load()["settings"] == before["settings"]
        assert ro.delivery_proof() == before["proof"]
        assert ro.get_meta("pendingSync") == before["claim_meta"]
        assert ro.sync_payload() == ro.sync_payload()


@pytest.mark.parametrize("boundary", ("second_write", "commit"))
def test_add_failure_rolls_back_task_selection_retarget_and_claim(task_window, boundary):
    hw = task_window
    hw.claim = hw.store.sync_payload()
    title = f"R48 D02 add {hw.focus} {boundary}"
    task = task_from_title(title)
    h = _harness(hw.store, title)
    before = _fail_add(h, boundary, title)
    assert before["claim_meta"] == hw.claim
    assert hw.store.sync_payload() == hw.claim
    _reopen_equals(hw.path, before)
    assert task["id"] not in {t["taskId"] for t in hw.store.load()["pendingTasks"]}
    # retry succeeds once
    h2 = _harness(hw.store, title)
    out = h2.controller.add_task(title)
    assert any(isinstance(e, Synchronize) for e in out.effects)
    assert h2.ctx.settings["selectedTaskId"] == task["id"]


@pytest.mark.parametrize("boundary", ("second_write", "commit"))
def test_delete_selected_failure_rolls_back_task_deselect_retarget_and_claim(task_window, boundary):
    hw = task_window
    doomed = task_from_title(f"R48 D02 doomed {hw.focus} {boundary}")
    hw.store.queue_task_operation("upsert", doomed)
    hw.store.set_selected_task_id(doomed["id"])
    if hw.focus == "active":
        pass  # timer already running from fixture
    hw.claim = hw.store.sync_payload()
    h = _harness(hw.store)
    assert h.ctx.settings.get("selectedTaskId") == doomed["id"]
    before = _fail_delete_selected(h, boundary, doomed["id"])
    assert hw.store.sync_payload() == hw.claim
    _reopen_equals(hw.path, before)
    # retry succeeds once
    h2 = _harness(hw.store)
    out = h2.controller.delete_task(doomed["id"])
    assert any(isinstance(e, Synchronize) for e in out.effects)
    assert h2.ctx.settings.get("selectedTaskId") is None


def _assert_success_identities(hw, before: dict, task: dict, selected_id: str | None, op_type: str):
    st = hw.store.load()
    new_tasks = [t for t in st["pendingTasks"] if t not in before["tasks"]]
    new_sel = [t for t in st["pendingSelectedTasks"] if t not in before["selected"]]
    assert len(new_tasks) == 1 and new_tasks[0]["taskId"] == task["id"] and new_tasks[0]["type"] == op_type
    assert len(new_sel) == 1 and new_sel[0]["taskId"] == selected_id
    assert new_tasks[0]["id"] != new_sel[0]["id"]
    proof = hw.store.delivery_proof()
    assert new_tasks[0]["id"] in proof["taskOperations"]
    assert new_sel[0]["id"] in proof["selectedTaskOperations"]
    never = hw.store.never_sent_claim({})
    assert new_tasks[0]["id"] in never["taskOperations"]
    assert new_sel[0]["id"] in never["selectedTaskOperations"]
    if hw.focus == "active":
        new_cmds = [c for c in st["pending"] if c not in before["commands"]]
        assert len(new_cmds) == 1 and new_cmds[0]["type"] == "retarget"
        assert new_cmds[0].get("taskId") == selected_id
        assert new_cmds[0]["id"] in proof["commands"]
    else:
        assert st["pending"] == before["commands"]
    assert st["settings"].get("selectedTaskId") == selected_id
    assert hw.store.get_meta("hlc") != before["hlc"]
    assert hw.store.get_meta("lastUuidV7") != before["uuid"]
    claim = hw.store.sync_payload()
    flat = [i["id"] for d in ("commands", "taskOperations", "selectedTaskOperations") for i in claim.get(d, [])]
    assert new_tasks[0]["id"] in flat and new_sel[0]["id"] in flat
    retired_proof = hw.store.delivery_proof()
    with closing(Store(hw.path, shared_core=SharedCore(), iroh_secret_store=MemorySecretStore())) as ro:
        assert ro.load() == hw.store.load()
        assert ro.delivery_proof() == retired_proof
        assert ro.sync_payload() == claim


def test_add_success_commits_exact_queue_proof_identities_and_reopen(task_window):
    hw = task_window
    title = f"R48 D02 success add {hw.focus}"
    task = task_from_title(title)
    before = _snapshot(hw.store)
    h = _harness(hw.store, title)
    out = h.controller.add_task(title)
    assert any(isinstance(e, Synchronize) for e in out.effects)
    assert h.ctx.settings["selectedTaskId"] == task["id"]
    _assert_success_identities(hw, before, task, task["id"], "upsert")


def test_delete_selected_success_commits_exact_queue_proof_identities_and_reopen(task_window):
    hw = task_window
    doomed = task_from_title(f"R48 D02 success del {hw.focus}")
    hw.store.queue_task_operation("upsert", doomed)
    hw.store.set_selected_task_id(doomed["id"])
    before = _snapshot(hw.store)
    h = _harness(hw.store)
    assert h.ctx.settings.get("selectedTaskId") == doomed["id"]
    out = h.controller.delete_task(doomed["id"])
    assert any(isinstance(e, Synchronize) for e in out.effects)
    assert h.ctx.settings.get("selectedTaskId") is None
    _assert_success_identities(hw, before, doomed, None, "delete")
