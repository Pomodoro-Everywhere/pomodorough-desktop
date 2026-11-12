"""UX round 2 B7 (DSK-02): Tab is not trapped in the Iroh invite field.

Forward Tab leaves the invite field for JOIN ROOM and reaches every later
control; Shift+Tab leaves it backwards. Typing, Enter and paste still edit
the field, and a pasted ticket keeps its characters.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from test_r52_d06_task_header_wrap import _process, _tasks_window

LOCALES = ["en", "ar-XB"]
ROOM = {"roomId": "room-12345678", "roomName": "Platform 9", "peerCount": 1}
TAB_LIMIT = 16
CONTROLS = (
    "replication_mode_combo",
    "room_name_input",
    "create_room_button",
    "invite_input",
    "join_room_button",
    "invite_output",
    "copy_invite_button",
    "refresh_invite_button",
    "sync_iroh_button",
    "leave_room_button",
    "privacy_policy_button",
    "delete_account_button",
)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    yield QApplication.instance() or QApplication([])


def _render(screen: NetworkScreen, room: dict | None, invite: str = "") -> None:
    screen.render(
        replication_mode="iroh",
        iroh_status="SYNC READY",
        iroh_details={},
        invite=invite,
        room=room,
        available=True,
        unavailable_reason="",
        cloud_authenticated=True,
        cloud_deleting_account=False,
    )


@contextmanager
def _network_screen(app, locale: str = "en", room: dict | None = None, invite: str = ""):
    screen = NetworkScreen(Strings(locale), "iroh")
    _render(screen, room, invite)
    screen.resize(800, 600)
    screen.show()
    screen.activateWindow()
    _process(app)
    try:
        yield screen
    finally:
        screen.close()
        screen.deleteLater()
        _process(app)


def _focus_invite_field(app, screen: NetworkScreen) -> None:
    screen.invite_input.setFocus()
    _process(app, 3)
    assert app.focusWidget() is screen.invite_input


def _control_name(screen: NetworkScreen, widget: QWidget | None) -> str:
    for name in CONTROLS:
        if getattr(screen, name) is widget:
            return name
    return type(widget).__name__


def _press(app, screen, key: Qt.Key, modifier=Qt.KeyboardModifier.NoModifier) -> str:
    """Send the key to the focused widget and name the control focused after."""
    QTest.keyClick(app.focusWidget(), key, modifier)
    _process(app, 3)
    return _control_name(screen, app.focusWidget())


def _tab_stops_after_invite_field(app, screen: NetworkScreen) -> list[str]:
    """Press Tab from the invite field until focus returns or the limit ends."""
    _focus_invite_field(app, screen)
    stops: list[str] = []
    for _ in range(TAB_LIMIT):
        stop = _press(app, screen, Qt.Key.Key_Tab)
        if stop in stops:
            break
        stops.append(stop)
    return stops


@pytest.mark.parametrize("locale", LOCALES)
def test_tab_moves_from_the_invite_field_to_join_room(app, locale):
    with _network_screen(app, locale) as screen:
        _focus_invite_field(app, screen)
        assert _press(app, screen, Qt.Key.Key_Tab) == "join_room_button"
        assert screen.invite_input.toPlainText() == ""


def test_tab_keeps_a_typed_invite_unchanged(app):
    with _network_screen(app) as screen:
        _focus_invite_field(app, screen)
        QTest.keyClicks(screen.invite_input, "ticket-abc")
        assert _press(app, screen, Qt.Key.Key_Tab) == "join_room_button"
        assert screen.invite_input.toPlainText() == "ticket-abc"


@pytest.mark.parametrize("locale", LOCALES)
def test_forward_tab_reaches_the_later_controls_without_a_room(app, locale):
    with _network_screen(app, locale) as screen:
        stops = _tab_stops_after_invite_field(app, screen)
        expected = [
            "join_room_button",
            "invite_output",
            "privacy_policy_button",
            "delete_account_button",
        ]
        assert stops[: len(expected)] == expected
        assert screen.invite_input.toPlainText() == ""


def test_forward_tab_reaches_the_room_controls(app):
    with _network_screen(app, room=ROOM, invite="ticket-abc") as screen:
        stops = _tab_stops_after_invite_field(app, screen)
        expected = [
            "invite_output",
            "copy_invite_button",
            "refresh_invite_button",
            "sync_iroh_button",
            "leave_room_button",
            "privacy_policy_button",
            "delete_account_button",
        ]
        assert stops[: len(expected)] == expected
        assert screen.invite_output.toPlainText() == "ticket-abc"


def test_shift_tab_leaves_the_invite_field_and_tab_returns(app):
    with _network_screen(app) as screen:
        _focus_invite_field(app, screen)
        shift = Qt.KeyboardModifier.ShiftModifier
        assert _press(app, screen, Qt.Key.Key_Backtab, shift) == "create_room_button"
        assert _press(app, screen, Qt.Key.Key_Tab) == "invite_input"
        assert _press(app, screen, Qt.Key.Key_Tab) == "join_room_button"
        assert _press(app, screen, Qt.Key.Key_Backtab, shift) == "invite_input"
        assert screen.invite_input.toPlainText() == ""


def test_typing_and_enter_still_edit_the_invite_field(app):
    with _network_screen(app) as screen:
        _focus_invite_field(app, screen)
        QTest.keyClicks(screen.invite_input, "first")
        _press(app, screen, Qt.Key.Key_Return)
        QTest.keyClicks(screen.invite_input, "second")
        _press(app, screen, Qt.Key.Key_Backspace)
        assert app.focusWidget() is screen.invite_input
        assert screen.invite_input.toPlainText() == "first\nsecon"


def test_pasted_ticket_keeps_its_characters_and_joins(app):
    # The offscreen platform keeps its clipboard in this process.
    ticket = "pomodorough-ticket\twith tab\nsecond line"
    with _network_screen(app) as screen:
        joined: list[str] = []
        screen.join_room_requested.connect(joined.append)
        QApplication.clipboard().setText(ticket)
        _focus_invite_field(app, screen)
        _press(app, screen, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        assert screen.invite_input.toPlainText() == ticket
        assert _press(app, screen, Qt.Key.Key_Tab) == "join_room_button"
        _press(app, screen, Qt.Key.Key_Space)
        assert joined == [ticket]


@pytest.mark.parametrize("locale", LOCALES)
def test_window_tab_order_passes_through_the_invite_field(app, tmp_path, locale):
    with _tasks_window(app, tmp_path, locale, 800, 600) as window:
        window._show_screen(3)
        screen = window.network_screen
        # The test profile has no Iroh runtime; show the panel as available.
        _render(screen, None)
        window.activateWindow()
        _process(app)
        stops = _tab_stops_after_invite_field(app, screen)
        assert stops[:2] == ["join_room_button", "invite_output"]
        assert "delete_account_button" in stops
        assert screen.invite_input.toPlainText() == ""
