"""UX pass: idle skip control, bounded controls panel, palette tokens.

The idle board shows Start plus a Core-decided Skip; active timers show
Finish and Cancel. Qt highlight blue is routed through railway tokens.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from PySide6.QtGui import QIcon, QPalette
from PySide6.QtWidgets import QApplication, QBoxLayout, QSystemTrayIcon
from test_ui import FakeCloud, MemorySecretStore

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from pomodorough.railway_palette import DANGER, STEEL, TRACK
from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.storage_completion import preview_plan_failure_count
from pomodorough.tasks_screen import TasksScreen
from pomodorough.timer_screen import TimerRenderState, TimerScreen
from pomodorough.ui import MainWindow
from pomodorough.ui_views import MainWindowViewMixin

SETTINGS = {
    "durations": {"focus": 25, "short_break": 5, "long_break": 15},
    "autoStartBreaks": False,
}
# Local noon keeps every seeded completion inside one Core calendar day,
# whatever the machine's zone offset.
NOW_MS = int(datetime(2026, 6, 1, 12, 0).astimezone().timestamp() * 1000)
# Start of the same local day; the Core calendar day for skips begins here.
UTC_PLUS_ONE = "<+01>-1"


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def core() -> SharedCore:
    return SharedCore()


@pytest.fixture
def screen(app) -> TimerScreen:
    return TimerScreen(Strings("en"), SETTINGS)


def _state(status: str, phase: str = "focus") -> TimerRenderState:
    timer = {"status": status, "phase": phase, "plannedDurationMs": 1_500_000}
    return TimerRenderState(
        source_timer=timer,
        display_timer=timer,
        status=status,
        elapsed=0,
        planned=1_500_000,
        remaining=1_500_000,
    )


def _render(screen: TimerScreen, status: str, **overrides) -> None:
    options = {
        "selected_phase": "focus",
        "mutations_enabled": True,
        "sound_active": False,
    }
    options.update(overrides)
    screen.render_controls(_state(status), **options)


def test_idle_board_shows_skip_and_hides_finish_cancel(screen) -> None:
    _render(screen, "idle", skip_destination="short_break")
    assert screen.skip_button.isVisibleTo(screen)
    assert screen.skip_button.isEnabled()
    assert screen.skip_button.text() == "SKIP TO SHORT BREAK"
    assert not screen.finish_button.isVisibleTo(screen)
    assert not screen.cancel_button.isVisibleTo(screen)


def test_active_board_shows_finish_cancel_and_hides_skip(screen) -> None:
    _render(screen, "running", skip_destination="short_break")
    assert not screen.skip_button.isVisibleTo(screen)
    assert screen.finish_button.isVisibleTo(screen)
    assert screen.cancel_button.isVisibleTo(screen)
    assert screen.finish_button.isEnabled()
    assert screen.cancel_button.isEnabled()


def test_skip_without_core_destination_is_disabled_not_hidden(screen) -> None:
    _render(screen, "idle", skip_destination=None)
    assert screen.skip_button.isVisibleTo(screen)
    assert not screen.skip_button.isEnabled()
    assert screen.skip_button.text() == "SKIP"


def test_skip_follows_mutation_fence(screen) -> None:
    _render(screen, "idle", skip_destination="focus", mutations_enabled=False)
    assert not screen.skip_button.isEnabled()
    assert screen.skip_button.text() == "SKIP TO FOCUS"


def test_button_block_height_is_stable_across_idle_and_active(screen) -> None:
    _render(screen, "idle", skip_destination="short_break")
    idle_height = screen.controls_column.sizeHint().height()
    _render(screen, "running")
    assert screen.controls_column.sizeHint().height() == idle_height


def test_task_picker_empty_label_is_no_task(screen) -> None:
    screen.render_task_selector(
        {"status": "idle", "phase": "focus"},
        False,
        selected_phase="focus",
        settings={"selectedTaskId": None},
        tasks=[],
        known_tasks={},
        mutations_enabled=True,
    )
    assert screen.task_combo.itemText(0) == "No task"


def test_controls_width_caps_derive_from_screen_width(screen) -> None:
    screen.resize(1100, 620)
    screen.apply_responsive_layout(landscape=True, compact=False)
    need = screen.controls_column.minimumSizeHint().width()
    assert screen.controls_panel.maximumWidth() == max(need, 1100 * 2 // 5)
    assert screen.controls_column.maximumWidth() == max(need, 1100 * 3 // 5)
    screen.resize(640, 300)
    screen.apply_responsive_layout(landscape=True, compact=True)
    need = screen.controls_column.minimumSizeHint().width()
    # B2 L3: the panel also holds the vertical bar when the column overflows.
    bar = screen.controls_scroll.bar_allowance()
    assert screen.controls_panel.maximumWidth() == max(need + bar, 640 * 2 // 5)
    screen.apply_responsive_layout(landscape=False, compact=True)
    assert screen.controls_panel.maximumWidth() == 16777215
    assert screen.controls_column.maximumWidth() == max(need, 640 * 3 // 5)


def test_timer_screen_stylesheet_frames_controls_panel(screen) -> None:
    assert screen.controls_column.objectName() == "controlsPanel"
    assert "QFrame#controlsPanel" in screen.stylesheet()


def _focus_item(index: int, completed_at: datetime, phase: str = "focus") -> dict:
    stamp = completed_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": f"item-{index}",
        "timerId": f"timer-{index}",
        "phase": phase,
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "completedAt": stamp,
        "endedAt": stamp,
    }


def _history_before(now_ms: int, count: int) -> list[dict]:
    base = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    return [
        _focus_item(index, base - timedelta(minutes=30 * (index + 1)))
        for index in range(count)
    ]


def _today_history(count: int) -> list[dict]:
    return _history_before(NOW_MS, count)


@pytest.fixture
def store(tmp_path, core) -> Iterator[Store]:
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=core,
        iroh_secret_store=MemorySecretStore(),
    )
    yield store
    store.close()


def test_store_skip_destination_matches_core_long_break_rule(store) -> None:
    assert store.skip_destination("focus", [], NOW_MS) == "short_break"
    assert store.skip_destination("focus", _today_history(3), NOW_MS) == "long_break"
    assert store.skip_destination("focus", _today_history(4), NOW_MS) == "short_break"
    assert store.skip_destination("short_break", _today_history(3), NOW_MS) == "focus"
    assert store.skip_destination("long_break", [], NOW_MS) == "focus"


def test_store_skip_destination_strips_client_pending_marker(store) -> None:
    history = _today_history(3)
    history[0]["pending"] = True
    assert store.skip_destination("focus", history, NOW_MS) == "long_break"


def test_store_skip_destination_rejects_invalid_history_without_raising(store) -> None:
    before = preview_plan_failure_count()
    broken = _today_history(1)
    broken[0]["plannedDurationMs"] = 1
    assert store.skip_destination("focus", broken, NOW_MS) is None
    assert preview_plan_failure_count() == before + 1
    assert store.skip_destination("unknown", [], NOW_MS) is None


@pytest.fixture
def window(tmp_path, app, core):
    store = Store(
        tmp_path / "window.sqlite3",
        shared_core=core,
        iroh_secret_store=MemorySecretStore(),
    )
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        view = MainWindow(store, FakeCloud(authenticated=False), QIcon(), locale="en")
    view.tick_timer.stop()
    view.sync_timer.stop()
    yield view, store
    view.quitting = True
    view.close()
    view.deleteLater()
    app.processEvents()
    store.close()


@pytest.fixture
def local_zone(request) -> Iterator[None]:
    zone = request.param
    if zone is None:
        yield
        return
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only; Windows cannot pin the local zone in-process")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        assert datetime(2026, 6, 1).astimezone().utcoffset() != timedelta(0)
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_window_skip_button_selects_core_destination(window) -> None:
    view, store = window
    assert view._current_timer()["status"] == "idle"
    view._render()
    assert view.skip_button.isEnabled()
    assert view.skip_button.text() == "SKIP TO SHORT BREAK"
    view.skip_button.click()
    assert view._selected_phase() == "short_break"
    assert store.load()["settings"]["selectedPhase"] == "short_break"
    assert view.skip_button.text() == "SKIP TO FOCUS"
    view.skip_button.click()
    assert view._selected_phase() == "focus"


def test_window_skip_recomputes_after_history_changes(window) -> None:
    view, store = window
    # R54-D09: on the wall clock, before 01:30 local time two of the three
    # sessions fall on the previous day, so pin the window clock to local noon.
    with patch.object(store, "effective_timer_now_ms", return_value=NOW_MS):
        view._render()
        assert view.skip_button.text() == "SKIP TO SHORT BREAK"
        view.history = _today_history(3)
        view._render()
    assert view.skip_button.text() == "SKIP TO LONG BREAK"


@pytest.mark.parametrize(
    "local_zone", [None, UTC_PLUS_ONE], ids=["process-zone", "utc-plus-one"], indirect=True,
)
@pytest.mark.parametrize(
    ("minutes_after_midnight", "label"),
    [(45, "SKIP TO SHORT BREAK"), (105, "SKIP TO LONG BREAK")],
)
def test_window_skip_counts_only_sessions_since_local_midnight(
    local_zone, window, minutes_after_midnight, label
) -> None:
    # Sessions completed 30, 60 and 90 minutes earlier: at 00:45 two of them
    # belong to the previous local day and do not count toward a long break.
    view, store = window
    midnight_ms = int(datetime(2026, 6, 1).astimezone().timestamp() * 1000)
    now_ms = midnight_ms + minutes_after_midnight * 60_000
    view.history = _history_before(now_ms, 3)
    with patch.object(store, "effective_timer_now_ms", return_value=now_ms):
        view._render()
    assert view.skip_button.text() == label


def test_window_pins_highlight_palette_to_railway_tokens(window, app) -> None:
    palette = app.palette()
    assert palette.color(QPalette.ColorRole.Highlight).name() == STEEL
    assert palette.color(QPalette.ColorRole.HighlightedText).name() == TRACK


def test_composed_stylesheet_has_no_qt_highlight_and_styles_disabled(window) -> None:
    view, _store = window
    sheets = [
        MainWindowViewMixin._stylesheet(),
        view.timer_screen.stylesheet(),
        TasksScreen.stylesheet(),
        ArrivalsScreen.stylesheet(),
        NetworkScreen.stylesheet(),
    ]
    composed = "\n".join(sheets)
    assert "palette(highlight)" not in composed
    assert "palette(highlighted-text)" not in composed
    assert "@" not in composed.replace("@media", "")
    assert "QPushButton#dangerButton:disabled" in composed
    assert "QPushButton#primaryButton:disabled" in composed
    assert f"QPushButton#dangerButton {{ color: {DANGER}" in composed


def test_dial_focus_ring_uses_signal_token(window) -> None:
    import inspect

    from pomodorough import timer_view

    source = inspect.getsource(timer_view)
    assert "ColorRole.Highlight" not in source


def test_network_account_row_sits_last_with_delete_final(window) -> None:
    view, _store = window
    screen = view.network_screen
    layout = screen.privacy_policy_button.parentWidget().layout()
    row = None
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.layout() is not None and item.layout().indexOf(
            screen.delete_account_button
        ) >= 0:
            row = index
    assert row == layout.count() - 1
    account_row: QBoxLayout = layout.itemAt(row).layout()
    assert account_row.indexOf(screen.privacy_policy_button) < account_row.indexOf(
        screen.delete_account_button
    )
    assert screen.delete_account_button.objectName() == "dangerButton"


@pytest.mark.parametrize("size", [(640, 360), (1100, 720)])
def test_window_controls_panel_stays_bounded_and_visible(window, app, size) -> None:
    view, _store = window
    view.resize(*size)
    view.show()
    for _ in range(6):
        app.processEvents()
    screen = view.timer_screen
    column = screen.controls_column
    width, _height = size
    assert column.width() <= max(column.minimumSizeHint().width(), width * 3 // 5)
    if width > 780:
        assert screen.controls_panel.width() <= max(
            column.minimumSizeHint().width(), width * 2 // 5
        )
    for control in (screen.task_combo, screen.primary_button, screen.skip_button):
        assert not control.visibleRegion().isEmpty()
        assert column.rect().contains(control.geometry())
    assert column.rect().contains(screen.task_selector_panel.geometry())
    view.hide()
