"""UX round 2 B5: an unfinished run does not read as time spent.

History keeps only the planned duration. A cancelled or superseded row
shows its status and words that number as planned. Completed rows are
unchanged until Core records elapsed time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import resources
from string import Formatter

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.localization import Strings
from test_r52_d06_task_header_wrap import _process, _tasks_window

LOCALES = ["en", "ar-XB"]
UNFINISHED = ["cancelled", "superseded"]
PLANNED_WORD = {"en": "planned 25 min", "ar-XB": "plånnëd 25 mïn"}
SPENT_FORM = {"en": "  25 min", "ar-XB": "  25 mïn"}
TASKS = {"task-1": {"id": "task-1", "title": "Architecture"}}


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _arrival(status: str, **changes: object) -> dict[str, object]:
    arrival: dict[str, object] = {
        "id": f"history-{status}",
        "taskId": "task-1",
        "phase": "focus",
        "status": status,
        "plannedDurationMs": 1_500_000,
        "endedAt": "2026-01-01T12:00:00Z",
    }
    arrival.update(changes)
    return arrival


def _rows(screen: ArrivalsScreen) -> list[str]:
    items = [screen.history_list.item(row) for row in range(screen.history_list.count())]
    return [item.text() for item in items]


def _status(locale: str, status: str) -> str:
    return Strings(locale).text(f"arrivals.status.{status}")


def _catalog(locale: str) -> dict[str, str]:
    name = f"strings.{locale}.json"
    text = resources.files("pomodorough.resources").joinpath(name).read_text("utf-8")
    return json.loads(text)


def _fields(template: str) -> set[str]:
    return {name for _text, name, _spec, _conv in Formatter().parse(template) if name}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("status", UNFINISHED)
def test_unfinished_row_words_its_minutes_as_planned(app, locale, status):
    screen = ArrivalsScreen(Strings(locale), "device-12345678")
    screen.render([_arrival(status)], TASKS)
    (row,) = _rows(screen)
    assert _status(locale, status) in row
    assert PLANNED_WORD[locale] in row
    assert SPENT_FORM[locale] not in row
    assert "Architecture" in row


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("status", UNFINISHED)
def test_screen_reader_text_says_planned(app, locale, status):
    screen = ArrivalsScreen(Strings(locale), "device-12345678")
    screen.render([_arrival(status, pending=True)], TASKS)
    item = screen.history_list.item(0)
    spoken = item.data(Qt.ItemDataRole.AccessibleTextRole)
    assert PLANNED_WORD[locale] in spoken
    assert _status(locale, status) in spoken
    assert "\n" not in spoken
    assert Strings(locale).text("arrivals.pending").strip() in spoken


@pytest.mark.parametrize("locale", LOCALES)
def test_completed_row_is_unchanged(app, locale):
    strings = Strings(locale)
    screen = ArrivalsScreen(strings, "device-12345678")
    arrival = _arrival("completed", completedAt="2026-01-01T12:00:00Z")
    screen.render([arrival], TASKS)
    expected = strings.text(
        "arrivals.row",
        phase=strings.text("phase.focus"),
        status=_status(locale, "completed"),
        task="Architecture",
        minutes=25,
        when=screen._time_label("2026-01-01T12:00:00Z"),
        pending="",
    )
    assert _rows(screen) == [expected]
    assert PLANNED_WORD[locale] not in expected


def test_mixed_history_marks_only_unfinished_rows(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    history = [
        _arrival("completed", plannedDurationMs=300_000),
        _arrival("cancelled", plannedDurationMs=600_000),
        _arrival("superseded", plannedDurationMs=900_000, phase="short_break"),
    ]
    screen.render(history, TASKS)
    rows = _rows(screen)
    assert ["planned" in row for row in rows] == [False, True, True]
    assert "Completed  5 min" in rows[0]
    assert "Cancelled  planned 10 min" in rows[1]
    assert "Superseded  planned 15 min" in rows[2]


def test_row_rewords_when_the_status_changes(app):
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    screen.render([_arrival("cancelled", id="history-1")], TASKS)
    assert "planned 25 min" in _rows(screen)[0]
    screen.render([_arrival("completed", id="history-1")], TASKS)
    assert "planned" not in _rows(screen)[0]


@pytest.mark.parametrize("locale", LOCALES)
def test_window_history_uses_the_planned_wording(app, tmp_path, locale):
    with _tasks_window(app, tmp_path, locale, 640, 360) as window:
        window.known_tasks.update(TASKS)
        window.history = [_arrival("cancelled"), _arrival("completed")]
        window._render_history()
        _process(app)
        rows = [window.history_list.item(row).text() for row in range(2)]
        assert [PLANNED_WORD[locale] in row for row in rows] == [True, False]


@pytest.mark.parametrize("locale", LOCALES)
def test_planned_row_key_is_new_and_keeps_the_row_fields(locale):
    catalog = _catalog(locale)
    assert _fields(catalog["arrivals.row_planned"]) == _fields(catalog["arrivals.row"])
    assert catalog["arrivals.row_planned"] != catalog["arrivals.row"]


def test_existing_row_key_keeps_its_text():
    # The completed-row sentence is a shipped key; it is not reused.
    assert _catalog("en")["arrivals.row"] == (
        "{phase} · {task} · {status}  {minutes} min\n{when}{pending}"
    )
    assert _catalog("ar-XB")["arrivals.row"] == (
        "⟦‼ {phase} · {task} · {status}  {minutes} mïn\n{when}{pending} ‼⟧"
    )
