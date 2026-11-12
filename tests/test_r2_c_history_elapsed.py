"""UX round 2 batch C: history shows the time a session really ran.

Core 0.55.0 records ``elapsedMs`` on every history row it writes. Desktop
stores and replicates that value unchanged, validates its range, and shows
it in rows and task totals. A row stored before the field existed still
loads, and Desktop never writes a value of its own for it.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime
from importlib import resources
from string import Formatter
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from pomodorough import shared_core
from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.cli import _print_history
from pomodorough.arrival_minutes_wording import arrival_minutes
from pomodorough.core import recorded_elapsed_ms, task_from_title, task_summaries_today
from pomodorough.iroh_protocol import IrohProtocolError, canonical_json, validate_record
from pomodorough.localization import Strings
from pomodorough.shared_core import SharedCore, SharedCoreABIError
from pomodorough.storage import Store
from pomodorough.storage_canonical_reconciliation import core_history
from pomodorough.storage_replication_projection import ReplicatedStateProjection
from pomodorough.timer_screen import TimerScreen
from pomodorough.tui import _history_lines
import test_storage
from test_ui import MemorySecretStore

LOCALES = ["en", "ar-XB"]
FINISHED_EARLY = {"en": "Finished early", "ar-XB": "⟦‼ Fïnïshëd ëårly ‼⟧"}
MINUTES = {"en": "{} min", "ar-XB": "{} mïn"}
REMAINING = {"en": "remaining", "ar-XB": "rëmåïnïng"}
PLANNED_MS = 1_500_000
SEVEN_MINUTES_MS = 420_000
TASK = task_from_title("Architecture")
TASKS = {TASK["id"]: TASK}


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _row(status: str = "completed", **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": f"history-{status}",
        "timerId": f"timer-{status}",
        "taskId": TASK["id"],
        "phase": "focus",
        "status": status,
        "plannedDurationMs": PLANNED_MS,
    }
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    row["completedAt" if status == "completed" else "endedAt"] = stamp
    row.update(changes)
    return row


def _rows(screen: ArrivalsScreen) -> list[str]:
    widget = screen.history_list
    return [widget.item(index).text() for index in range(widget.count())]


def _catalog(locale: str) -> dict[str, str]:
    name = f"strings.{locale}.json"
    text = resources.files("pomodorough.resources").joinpath(name).read_text("utf-8")
    return json.loads(text)


def _fields(template: str) -> set[str]:
    return {name for _text, name, _spec, _conv in Formatter().parse(template) if name}


# An early Finish shows its real time in history and totals.


def test_early_finish_records_the_real_elapsed_time_from_core(app, tmp_path):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    try:
        start_ms = int(time.time() * 1000) - 600_000
        durations = store.load()["settings"]["durationsMs"]
        store.queue_task_operation_with_selection(
            "upsert", TASK, TASK["id"], now_ms=start_ms - 1_000
        )
        store.queue_command("start", None, "focus", durations, now_ms=start_ms)
        running = store.projected_state(now_ms=start_ms + 1_000).canonical_timer
        store.queue_command(
            "finish", running, "focus", durations, now_ms=start_ms + SEVEN_MINUTES_MS
        )
        projection = store.projected_state(now_ms=start_ms + 500_000)
    finally:
        store.close()
    (row,) = projection.history
    assert (row["status"], row["plannedDurationMs"]) == ("completed", PLANNED_MS)
    assert row["elapsedMs"] == SEVEN_MINUTES_MS
    totals = task_summaries_today(projection.tasks, projection.history)
    assert totals[TASK["id"]] == {"finished": 1, "timeMs": SEVEN_MINUTES_MS}
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render(projection.history, TASKS)
    (text,) = _rows(screen)
    assert "Finished early  7 min" in text
    assert "25 min" not in text


def test_task_total_counts_elapsed_of_completed_rows_only():
    history = [
        _row("completed", id="early", timerId="t-early", elapsedMs=SEVEN_MINUTES_MS),
        _row("completed", id="old", timerId="t-old"),
        _row("completed", id="null", timerId="t-null", elapsedMs=None),
        _row("cancelled", elapsedMs=600_000),
        _row("superseded", elapsedMs=300_000),
    ]
    totals = task_summaries_today([TASK], history)
    # A row stored before the field existed counts its plan, as Core does.
    assert totals[TASK["id"]] == {
        "finished": 3,
        "timeMs": SEVEN_MINUTES_MS + 2 * PLANNED_MS,
    }


@pytest.mark.parametrize("locale", LOCALES)
def test_finished_early_row_shows_its_real_minutes(app, locale):
    screen = ArrivalsScreen(Strings(locale), "device-12345678")
    screen.render([_row(elapsedMs=SEVEN_MINUTES_MS)], TASKS)
    (text,) = _rows(screen)
    assert f"{FINISHED_EARLY[locale]}  {MINUTES[locale].format(7)}" in text
    assert MINUTES[locale].format(25) not in text


def test_rows_with_elapsed_show_the_time_that_ran(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render(
        [
            _row("completed", id="a", timerId="ta", elapsedMs=PLANNED_MS),
            _row("cancelled", id="b", timerId="tb", elapsedMs=629_999),
            _row("superseded", id="c", timerId="tc", elapsedMs=630_000),
        ],
        TASKS,
    )
    texts = sorted(_rows(screen))
    assert any("Completed  25 min" in text for text in texts)
    assert any("Cancelled  10 min" in text for text in texts)
    assert any("Superseded  11 min" in text for text in texts)
    assert not any("planned" in text or "Finished early" in text for text in texts)


@pytest.mark.parametrize("elapsed", ["missing", None])
def test_row_without_elapsed_keeps_the_planned_wording(app, elapsed):
    def row(status: str) -> dict[str, Any]:
        item = _row(status, id=status, timerId=f"t-{status}")
        if elapsed is None:
            item["elapsedMs"] = None
        return item

    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([row("completed"), row("cancelled")], TASKS)
    texts = sorted(_rows(screen))
    assert any("Completed  25 min" in text for text in texts)
    assert any("Cancelled  planned 25 min" in text for text in texts)
    assert not any("Finished early" in text for text in texts)


def test_arrivals_rerender_when_only_elapsed_changes(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([_row(elapsedMs=SEVEN_MINUTES_MS)], TASKS)
    screen.render([_row(elapsedMs=600_000)], TASKS)
    (text,) = _rows(screen)
    assert "Finished early  10 min" in text


def test_cli_history_row_shows_finished_early_minutes():
    class Timer:
        def retained_history(self, _limit: int) -> list[dict[str, Any]]:
            return [_row(elapsedMs=SEVEN_MINUTES_MS)]

    stream = io.StringIO()
    args = argparse.Namespace(limit=5, as_json=False)
    _print_history(args, Timer(), stream, Strings("en"))
    assert "| Finished early | 7 min" in stream.getvalue()


def test_tui_history_row_shows_finished_early_minutes():
    (line,) = _history_lines([_row(elapsedMs=SEVEN_MINUTES_MS)], 80, Strings("en"))
    assert "| Finished early | 7 min" in line


@pytest.mark.parametrize("locale", LOCALES)
def test_finished_early_key_is_catalogued(locale):
    catalog = _catalog(locale)
    assert catalog["arrivals.status.finished_early"] == FINISHED_EARLY[locale]


# A row without the field loads, and nothing writes a default back.


def test_store_accepts_rows_with_missing_null_or_in_range_elapsed():
    for value in ("missing", None, 0, SEVEN_MINUTES_MS, PLANNED_MS):
        item = _row()
        if value != "missing":
            item["elapsedMs"] = value
        assert Store._valid_history_item(item), value


def test_reconciliation_input_keeps_elapsed_and_never_invents_it():
    rows = [
        _row(id="a", timerId="ta", elapsedMs=SEVEN_MINUTES_MS),
        _row(id="b", timerId="tb", elapsedMs=None),
        _row(id="c", timerId="tc"),
    ]
    normalized = core_history(rows)
    assert normalized[0]["elapsedMs"] == SEVEN_MINUTES_MS
    assert "elapsedMs" not in normalized[1]
    assert "elapsedMs" not in normalized[2]


def test_replicated_history_keeps_elapsed_and_drops_null():
    rows = [
        _row(id="a", timerId="ta", elapsedMs=SEVEN_MINUTES_MS),
        _row(id="b", timerId="tb", elapsedMs=None),
    ]
    cleaned = {item["id"]: item for item in ReplicatedStateProjection.clean_history(rows)}
    assert cleaned["a"]["elapsedMs"] == SEVEN_MINUTES_MS
    assert "elapsedMs" not in cleaned["b"]


# A sync response carries the field through to the stored history.


def _synced_history(tmp_path, elapsed: Any) -> tuple[list[dict[str, Any]], Exception | None]:
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    try:
        request = store.sync_payload()
        row = _row(id="synced", timerId="t-synced")
        if elapsed != "missing":
            row["elapsedMs"] = elapsed
        # A module import: a TestCase class in this namespace would run here.
        response = test_storage.StorageTests._canonical_response(request, history=[row])
        try:
            store.apply_sync(response, request)
        except ValueError as error:
            return store.load()["snapshot"]["history"], error
        return store.load()["snapshot"]["history"], None
    finally:
        store.close()


@pytest.mark.parametrize("elapsed", [0, SEVEN_MINUTES_MS, PLANNED_MS])
def test_sync_response_elapsed_is_stored_as_sent(tmp_path, elapsed):
    (stored,), error = _synced_history(tmp_path, elapsed)
    assert error is None
    assert stored["elapsedMs"] == elapsed
    assert task_summaries_today([TASK], [stored])[TASK["id"]]["timeMs"] == elapsed


@pytest.mark.parametrize("elapsed", ["missing", None])
def test_sync_response_without_elapsed_reads_as_planned(tmp_path, elapsed):
    (stored,), error = _synced_history(tmp_path, elapsed)
    assert error is None
    assert recorded_elapsed_ms(stored) is None
    assert arrival_minutes(stored).minutes == PLANNED_MS // 60_000


@pytest.mark.parametrize("elapsed", [-1, PLANNED_MS + 1, True, "420000"])
def test_sync_response_with_out_of_range_elapsed_fails_closed(tmp_path, elapsed):
    history, error = _synced_history(tmp_path, elapsed)
    assert str(error) == "Server returned invalid timer history."
    assert history == []


# An out-of-range value fails closed.

OUT_OF_RANGE = [-1, PLANNED_MS + 1, True, "420000", 420_000.0]


@pytest.mark.parametrize("value", OUT_OF_RANGE)
def test_store_rejects_out_of_range_elapsed(value):
    assert not Store._valid_history_item(_row(elapsedMs=value))


@pytest.mark.parametrize("value", OUT_OF_RANGE)
def test_core_output_with_out_of_range_elapsed_fails_closed(value):
    with pytest.raises(SharedCoreABIError, match=r"history\[0\]\.elapsedMs"):
        shared_core._validated_history([_row(elapsedMs=value)])


def test_core_output_with_null_elapsed_fails_closed():
    # Core omits the field for a row without it; it never writes null.
    error = "malformed projection.apply.v2 output: history[0].elapsedMs is not an integer"
    with pytest.raises(SharedCoreABIError, match=f"^{re.escape(error)}$"):
        shared_core._validated_history([_row(elapsedMs=None)])


def test_core_output_with_in_range_elapsed_passes():
    rows = [_row(elapsedMs=0), _row("cancelled", elapsedMs=PLANNED_MS)]
    assert shared_core._validated_history(rows) == rows


# The Iroh genesis codec round-trips the field.


def _genesis(history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "domain": "genesis",
        "deviceId": "01a0219e-0800-7000-8000-000000000000",
        "operation": {
            "canonicalTimer": None,
            "history": history,
            "tasks": [TASK],
            "durationsMs": {"focus": PLANNED_MS, "short_break": 300_000,
                            "long_break": 900_000},
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "hlcWallMs": 1_787_270_400_000,
            "hlcCounter": 4,
        },
    }


def _iroh_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "01a0219e-0800-7002-8000-000000000002",
        "timerId": "01a0219e-0800-7002-8000-000000000002",
        "commandId": "01a0219e-0800-7002-8000-000000000003",
        "taskId": TASK["id"],
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": PLANNED_MS,
        "completedAt": "2026-08-21T00:07:00Z",
        "elapsedMs": SEVEN_MINUTES_MS,
    }
    row.update(changes)
    return row


def test_genesis_history_round_trips_elapsed():
    record = _genesis([_iroh_row()])
    decoded = json.loads(canonical_json(validate_record(deepcopy(record))))
    assert validate_record(decoded) == record
    assert decoded["operation"]["history"][0]["elapsedMs"] == SEVEN_MINUTES_MS


def test_genesis_history_without_elapsed_still_decodes():
    row = _iroh_row()
    row.pop("elapsedMs")
    record = _genesis([row])
    assert "elapsedMs" not in validate_record(record)["operation"]["history"][0]


GENESIS_NOT_CANONICAL = "Genesis history is not canonical."
GENESIS_INVALID = "Genesis history is invalid."
# A float never reaches the history check: canonical JSON rejects it first.
CANONICAL_JSON = "Record contains unsupported canonical JSON values."


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (None, GENESIS_NOT_CANONICAL),
        (-1, GENESIS_INVALID),
        (PLANNED_MS + 1, GENESIS_INVALID),
        (True, GENESIS_INVALID),
        ("420000", GENESIS_INVALID),
        (420_000.0, CANONICAL_JSON),
    ],
)
def test_genesis_history_rejects_null_or_out_of_range_elapsed(value, error):
    with pytest.raises(IrohProtocolError, match=f"^{re.escape(error)}$"):
        validate_record(_genesis([_iroh_row(elapsedMs=value)]))


# A timer finished early shows as ended.


def _early_finished_timer() -> dict[str, Any]:
    return {
        "id": "timer-early",
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": PLANNED_MS,
        "elapsedAtAnchorMs": SEVEN_MINUTES_MS,
        "anchorAt": "2026-08-21T00:07:00Z",
        "lastIntent": {"type": "finish", "commandId": "c", "occurredAt": "2026-08-21T00:07:00Z"},
    }


@pytest.mark.parametrize("locale", LOCALES)
def test_early_finished_timer_shows_as_ended(app, locale):
    strings = Strings(locale)
    durations = {"focus": PLANNED_MS, "short_break": 300_000, "long_break": 900_000}
    screen = TimerScreen(strings, {"durations": {"focus": 25, "short_break": 5,
                                                 "long_break": 15}})
    state = screen.presentation(
        _early_finished_timer(), selected_phase="short_break",
        settings={"durationsMs": durations}, now_ms=0,
    )
    screen.render_clock(state, [])
    # The clock shows the next session at rest, not what was left of this one.
    assert (state.elapsed, state.remaining) == (0, 300_000)
    assert screen.clock.time_text == "05:00"
    assert screen.clock.progress == 0.0
    spoken = screen.clock.accessibleDescription()
    assert REMAINING[locale] not in spoken
    assert "05:00" in spoken
    assert screen.status_labels()["completed"].lower() in spoken


def test_running_timer_still_reads_as_remaining(app):
    strings = Strings("en")
    screen = TimerScreen(strings, {"durations": {"focus": 25, "short_break": 5,
                                                 "long_break": 15}})
    running = dict(_early_finished_timer(), status="running", lastIntent=None,
                   elapsedAtAnchorMs=0, anchorAt="1970-01-01T00:00:00Z")
    state = screen.presentation(
        running, selected_phase="focus",
        settings={"durationsMs": {"focus": PLANNED_MS, "short_break": 300_000,
                                  "long_break": 900_000}},
        now_ms=60_000,
    )
    screen.render_clock(state, [])
    assert "24:00 remaining" in screen.clock.accessibleDescription()


@pytest.mark.parametrize("locale", LOCALES)
def test_ended_description_keeps_the_description_fields(locale):
    catalog = _catalog(locale)
    assert _fields(catalog["status.timer_description_ended"]) == _fields(
        catalog["status.timer_description"]
    )
