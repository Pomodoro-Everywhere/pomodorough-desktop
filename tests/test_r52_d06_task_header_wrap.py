"""R52-D06: task explanation and daily totals wrap and reflow intact.

Qt 6.11.1 verified; allowed 6.8-6.12. Layout-only: no raw WASM envelope
here (covered by domain envelope proofs); this file asserts Qt geometry.
Fresh-label reference heights avoid circularity: QLabel.heightForWidth
honors the widget's own minimum height, so expectations come from a
detached label with the same text and font but no claimed minimum.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QLabel, QSystemTrayIcon
from unittest.mock import patch

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication, rounds: int = 10) -> None:
    for _ in range(rounds):
        app.processEvents()


@contextmanager
def _scaled_font(app: QApplication, point_size: float):
    previous = QFont(QApplication.font())
    scaled = QFont(previous)
    scaled.setPointSizeF(point_size)
    QApplication.setFont(scaled)
    _process(app)
    try:
        yield
    finally:
        QApplication.setFont(previous)
        _process(app)


@contextmanager
def _tasks_window(app: QApplication, tmp_path: Path, locale: str, width: int, height: int):
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=SharedCore(),
        iroh_secret_store=MemorySecretStore(),
    )
    cloud = FakeCloud(authenticated=True)
    with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
        window = MainWindow(store, cloud, QIcon(), locale=locale)
    window.tick_timer.stop()
    window.sync_timer.stop()
    window.resize(width, height)
    window.show()
    _process(app)
    window.screen_buttons[1].click()
    _process(app)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _render_nonzero(window: MainWindow) -> None:
    now = datetime.now().astimezone().isoformat()
    tasks = [{"id": "task-1", "title": "Task one"}]
    history = [
        {
            "id": f"history-{index}",
            "taskId": "task-1",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 1_500_000,
            "completedAt": now,
        }
        for index in range(2)
    ]
    window.tasks_screen.render(tasks, history, mutations_enabled=True)


def _fresh_need(label: QLabel) -> int:
    probe = QLabel(label.text())
    probe.setFont(QFont(label.font()))
    probe.setWordWrap(True)
    return probe.heightForWidth(max(1, label.width()))


def _header_labels(window: MainWindow) -> tuple[QLabel, QLabel, QLabel]:
    found = {
        label.objectName(): label
        for label in window.tasks_screen.findChildren(QLabel)
    }
    return (
        found["sectionTitle"],
        found["taskSubtitle"],
        found["countBadge"],
    )


def _assert_header_intact(window: MainWindow, where: str) -> None:
    page = window.tasks_screen
    for label in _header_labels(window):
        assert label.wordWrap(), f"{where}:{label.objectName()}"
        assert label.width() <= page.width() + 1, f"{where}:{label.objectName()}"
        assert label.height() + 1 >= _fresh_need(label), f"{where}:{label.objectName()}"


def _dump(store: Store) -> str:
    return "\n".join(store.connection.iterdump())


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("size", [(640, 360), (1200, 800)])
def test_header_wraps_intact_at_large_font(app, tmp_path, locale, size):
    with _scaled_font(app, 18.0):
        with _tasks_window(app, tmp_path, locale, *size) as window:
            _render_nonzero(window)
            _process(app, 15)
            _assert_header_intact(window, "grown")
            totals = window.task_totals.text()
            assert totals == window.tasks_screen.strings.text(
                "task.totals",
                count=2,
                unit=window.tasks_screen.strings.plural("task.pomodoro", 2),
                minutes=window.tasks_screen.strings.text(
                    "duration.minutes", minutes=50
                ).upper(),
            ), totals


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_live_font_growth_and_shrink_keep_intact(app, tmp_path, locale):
    with _scaled_font(app, 9.0):
        with _tasks_window(app, tmp_path, locale, 640, 360) as window:
            _render_nonzero(window)
            _process(app, 15)
            before = _dump(window.store)
            _assert_header_intact(window, "base")
            base_minimums = {
                label.objectName(): label.minimumHeight()
                for label in _header_labels(window)
            }
            grown = QFont(QApplication.font())
            grown.setPointSizeF(18.0)
            QApplication.setFont(grown)
            _process(app, 15)
            # No private reflow call; production deferred tick must reflow.
            _assert_header_intact(window, "grown")
            shrunk = QFont(QApplication.font())
            shrunk.setPointSizeF(9.0)
            QApplication.setFont(shrunk)
            _process(app, 15)
            _assert_header_intact(window, "shrunk")
            for label in _header_labels(window):
                assert (
                    label.minimumHeight()
                    <= base_minimums[label.objectName()] + 1
                ), label.objectName()
            assert _dump(window.store) == before
