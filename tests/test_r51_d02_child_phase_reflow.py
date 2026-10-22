"""R51-D02: child-only phase style changes schedule settings reflow.

Layout-only except one raw WASM envelope proof below; full decode
matrix lives in D01. This keeps Qt reflow green without duplicating
canonical-workspace coverage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from unittest.mock import patch

from pomodorough.shared_core import SharedCore
from pomodorough.storage import Store
from pomodorough.ui import MainWindow
from test_ui import FakeCloud, MemorySecretStore

CHILD_STYLE = "font-size: 30pt; padding: 3px 7px;"


class _ObservedCore(SharedCore):
    """Capture one raw WASM envelope to prove decode path."""

    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[dict] = []

    def dispatch_json(self, operation: str, input_json: str):
        raw: list[bytes] = []
        original = self._memory.read

        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            raw.append(bytes(result))
            return result

        with patch.object(self._memory, "read", side_effect=capture):
            value = super().dispatch_json(operation, input_json)
        assert len(raw) == 1
        decoded = json.loads(raw[0])
        assert decoded == {"ok": True, "value": value}
        self.envelopes.append({"operation": operation, "value": value})
        return value


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _process(app: QApplication, rounds: int = 6) -> None:
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
def _main_window(app: QApplication, tmp_path: Path, locale: str, width: int, height: int):
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
    window.settings_button.setChecked(True)
    _process(app, 8)
    try:
        yield window
    finally:
        window.quitting = True
        window.close()
        window.deleteLater()
        _process(app)
        store.close()


def _assert_phase_intact(window: MainWindow) -> None:
    assert len(window.timer_screen.phase_buttons) == 3
    for phase, button in window.timer_screen.phase_buttons.items():
        assert button.width() + 1 >= button.minimumSizeHint().width(), phase


def _dump(store: Store) -> str:
    return "\n".join(store.connection.iterdump())


def _widths(window: MainWindow) -> dict[str, int]:
    return {phase: b.width() for phase, b in window.timer_screen.phase_buttons.items()}


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("size", [(1200, 800), (640, 360)])
def test_child_style_growth_and_shrink_keep_intact(app, tmp_path, locale, size):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, *size) as window:
            _assert_phase_intact(window)
            grown = QFont(QApplication.font())
            grown.setPointSizeF(18.0)
            QApplication.setFont(grown)
            _process(app, 8)
            shrunk = QFont(QApplication.font())
            shrunk.setPointSizeF(9.0)
            QApplication.setFont(shrunk)
            _process(app, 8)
            before = _dump(window.store)
            short_break = window.timer_screen.phase_buttons["short_break"]
            short_break.setStyleSheet(CHILD_STYLE)
            _process(app, 10)
            _assert_phase_intact(window)
            assert _dump(window.store) == before
            short_break.setStyleSheet("")
            _process(app, 10)
            _assert_phase_intact(window)
            assert _dump(window.store) == before
            assert not window.timer_screen._settings_reflow_timer.isActive()


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
@pytest.mark.parametrize("size", [(1200, 800), (640, 360)])
def test_child_style_focus_and_delivery_stable(app, tmp_path, locale, size):
    with _scaled_font(app, 9.0):
        with _main_window(app, tmp_path, locale, *size) as window:
            short_break = window.timer_screen.phase_buttons["short_break"]
            short_break.setFocus(Qt.FocusReason.ShortcutFocusReason)
            _process(app)
            before = _dump(window.store)
            short_break.setStyleSheet(CHILD_STYLE)
            _process(app, 10)
            _assert_phase_intact(window)
            assert QApplication.focusWidget() is short_break
            settled = _widths(window)
            _process(app, 10)
            assert _widths(window) == settled
            got: list[str] = []
            window.timer_screen.phase_selected.connect(got.append)
            try:
                short_break.click()
                _process(app)
            finally:
                window.timer_screen.phase_selected.disconnect(got.append)
            assert got == ["short_break"]
            assert _dump(window.store) != before
            assert '"selectedPhase":"short_break"' in _dump(window.store)


def test_settings_load_decodes_raw_wasm_envelope(tmp_path) -> None:
    core = _ObservedCore()
    store = Store(
        tmp_path / "state.sqlite3",
        shared_core=core,
        iroh_secret_store=MemorySecretStore(),
    )
    try:
        assert isinstance(store.load(projection=True), dict)
        assert isinstance(store.sync_payload(), dict)
        assert store.projected_state(now_ms=1_786_000_000_000).durations_ms["focus"] == 25 * 60_000
        assert core.envelopes, "expected real WASM envelopes"
    finally:
        store.close()
