"""DSK-21: wording and consistency of dialogs, guidance and disabled controls.

The audit found Yes/Cancel on the leave-room and sign-out dialogs, the Iroh
first-room guidance shown while the Cloud route was active, disabled Network
buttons that never said why, a clock description that ran two sentences
together, and a README title that left out macOS.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from test_replication_controller import ControllerHarness, FakeIroh

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.destructive_confirmation import confirm_destructive_action
from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from pomodorough.timer_view import ClockWidget
from pomodorough.ui_views import MainWindowViewMixin

README = Path(__file__).resolve().parents[1] / "README.md"
ROOM = {"room_id": "room-1234567890", "name": "Team", "peers": 1, "records": 3}


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _ask(app: QApplication, choose: str | None) -> tuple[bool, dict[str, Any]]:
    """Run the confirmation with exec replaced by a scripted click."""
    seen: dict[str, Any] = {}

    def fake_exec(dialog: QMessageBox) -> int:
        destructive = [
            button
            for button in dialog.buttons()
            if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole
        ]
        cancel = dialog.button(QMessageBox.StandardButton.Cancel)
        seen.update(
            texts=sorted(button.text() for button in dialog.buttons()),
            destructive=[button.text() for button in destructive],
            default_is_cancel=dialog.defaultButton() is cancel,
            escape_is_cancel=dialog.escapeButton() is cancel,
            detail=dialog.informativeText(),
            title=dialog.text(),
        )
        target = {"confirm": destructive[0] if destructive else None, "cancel": cancel}
        if choose is not None and target[choose] is not None:
            target[choose].click()
        return 0

    with patch.object(QMessageBox, "exec", fake_exec):
        confirmed = confirm_destructive_action(
            None, title="Leave room?", detail="Details", action="Leave room"
        )
    return confirmed, seen


def test_confirmation_names_the_action_and_defaults_to_cancel(app) -> None:
    confirmed, seen = _ask(app, None)
    assert confirmed is False
    assert seen["destructive"] == ["Leave room"]
    assert not {"&Yes", "Yes", "&No", "No"} & set(seen["texts"])
    assert seen["default_is_cancel"] and seen["escape_is_cancel"]
    assert (seen["title"], seen["detail"]) == ("Leave room?", "Details")


@pytest.mark.parametrize(("choose", "expected"), [("confirm", True), ("cancel", False)])
def test_confirmation_reports_only_the_named_action(app, choose, expected) -> None:
    assert _ask(app, choose)[0] is expected


def test_leave_room_asks_with_a_named_action() -> None:
    harness = ControllerHarness(iroh=FakeIroh())
    harness.controller.mode = "iroh"
    harness.store.active_iroh_room_id = "room-1"
    ask = Mock(return_value=False)
    with patch("pomodorough.replication_controller.confirm_destructive_action", ask):
        assert harness.controller.leave_iroh_room().effects == ()
    assert ask.call_args.kwargs == {
        "title": "network.leave_title",
        "detail": "network.leave_detail",
        "action": "network.leave_confirm",
    }
    assert ("leave",) not in harness.store.calls


@pytest.mark.parametrize("locale", ["en", "ar-XB"])
def test_clock_description_separates_status_from_progress(app, locale) -> None:
    strings = Strings(locale)
    clock = ClockWidget(strings)
    clock.set_state("24:00", "focus", "idle", 0.0, dots=2)
    status = strings.text(
        "status.timer_description", phase="Focus", time="24:00", status="idle"
    )
    progress = strings.text("status.pomodoro_progress_value", count=2)
    assert clock.accessibleDescription() == strings.text(
        "status.timer_description_with_progress",
        description=status,
        progress=progress,
    )
    if locale == "en":
        assert clock.accessibleDescription() == (
            "Focus, 24:00 remaining, idle. 2 of 4 pomodoros completed today"
        )


def _render(screen: NetworkScreen, **overrides: Any) -> NetworkScreen:
    state: dict[str, Any] = {
        "replication_mode": "offline",
        "iroh_status": "NOT CONNECTED",
        "iroh_details": {},
        "invite": "",
        "room": None,
        "available": True,
        "unavailable_reason": "",
        "cloud_authenticated": False,
        "cloud_deleting_account": False,
    }
    state.update(overrides)
    screen.render(**state)
    return screen


@pytest.mark.parametrize(
    ("mode", "guided"), [("offline", True), ("iroh", True), ("centralized", False)]
)
def test_first_room_guidance_stays_off_the_cloud_route(app, mode, guided) -> None:
    screen = _render(NetworkScreen(Strings("en"), mode), replication_mode=mode)
    guidance = Strings("en").text("iroh.first_room_guidance")
    assert screen.iroh_first_room_guidance.isVisibleTo(screen) is guided
    assert (screen.replication_mode_combo.accessibleDescription() == guidance) is guided


def _choose_route(screen: NetworkScreen, mode: str) -> None:
    combo = screen.replication_mode_combo
    combo.setCurrentIndex(combo.findData(mode))


def test_asking_for_iroh_on_the_cloud_route_shows_the_guidance(app) -> None:
    screen = _render(NetworkScreen(Strings("en"), "centralized"), replication_mode="centralized")
    guidance = Strings("en").text("iroh.first_room_guidance")
    _choose_route(screen, "iroh")
    # The controller refuses the route and renders the Cloud route again.
    _render(screen, replication_mode="centralized")
    assert screen.iroh_first_room_guidance.isVisibleTo(screen)
    assert screen.replication_mode_combo.accessibleDescription() == guidance
    _choose_route(screen, "offline")
    _choose_route(screen, "centralized")
    _render(screen, replication_mode="centralized")
    assert not screen.iroh_first_room_guidance.isVisibleTo(screen)
    assert screen.replication_mode_combo.accessibleDescription() == ""


def test_a_usable_room_ends_the_cloud_route_guidance(app) -> None:
    screen = _render(NetworkScreen(Strings("en"), "centralized"), replication_mode="centralized")
    _choose_route(screen, "iroh")
    _render(screen, replication_mode="centralized", room={"roomId": "room-1"})
    assert not screen.iroh_first_room_guidance.isVisibleTo(screen)
    _render(screen, replication_mode="centralized")
    assert not screen.iroh_first_room_guidance.isVisibleTo(screen)


def _explanations(screen: NetworkScreen) -> dict[str, tuple[str, str]]:
    buttons = {
        "copy": screen.copy_invite_button,
        "refresh": screen.refresh_invite_button,
        "sync": screen.sync_iroh_button,
        "leave": screen.leave_room_button,
        "delete": screen.delete_account_button,
    }
    return {
        name: (button.toolTip(), button.accessibleDescription())
        for name, button in buttons.items()
    }


def test_disabled_network_buttons_say_why(app) -> None:
    strings = Strings("en")
    screen = _render(NetworkScreen(strings, "centralized"), replication_mode="centralized")
    room = strings.text("network.room_controls_disabled")
    invite = strings.text("network.copy_invite_disabled")
    signed_out = strings.text("account.delete_disabled_signed_out")
    assert _explanations(screen) == {
        "copy": (invite, invite),
        "refresh": (room, room),
        "sync": (room, room),
        "leave": (room, room),
        "delete": (signed_out, signed_out),
    }


def test_enabled_network_buttons_carry_no_reason(app) -> None:
    screen = _render(
        NetworkScreen(Strings("en"), "iroh"),
        replication_mode="iroh",
        room=ROOM,
        invite="pomodorough1.invite",
        cloud_authenticated=True,
    )
    assert set(_explanations(screen).values()) == {("", "")}


def test_unavailable_iroh_gives_the_unavailable_reason(app) -> None:
    screen = _render(
        NetworkScreen(Strings("en"), "offline"),
        available=False,
        unavailable_reason="Iroh is missing",
    )
    reasons = _explanations(screen)
    assert {reasons[name][0] for name in ("copy", "refresh", "sync", "leave")} == {
        "Iroh is missing"
    }


def test_join_and_deletion_in_progress_have_their_own_reasons(app) -> None:
    strings = Strings("en")
    screen = _render(
        NetworkScreen(strings, "iroh"),
        replication_mode="iroh",
        iroh_details={"joinPending": True},
        cloud_authenticated=True,
        cloud_deleting_account=True,
    )
    reasons = _explanations(screen)
    joining = strings.text("network.room_controls_disabled_joining")
    assert reasons["refresh"][0] == reasons["sync"][0] == joining
    assert reasons["leave"] == ("", "")
    assert reasons["delete"][0] == strings.text("account.delete_disabled_in_progress")


def _arrival(index: int, *, pending: bool) -> dict[str, Any]:
    return {
        "id": f"history-{index}",
        "timerId": f"timer-{index}",
        "taskId": None,
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "completedAt": "2026-01-01T12:00:00Z",
        "pending": pending,
    }


@pytest.mark.parametrize(
    ("pending", "sync_route_active", "hinted"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_arrivals_say_what_starts_the_sync(app, pending, sync_route_active, hinted) -> None:
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    history = [_arrival(0, pending=False), _arrival(1, pending=pending)]
    screen.render(history, {}, sync_route_active=sync_route_active)
    assert screen.sync_hint.isVisibleTo(screen) is hinted
    assert screen.sync_hint.text() == (
        "Pending arrivals sync after you sign in to Pomodorough Cloud or join an Iroh room."
    )


def test_arrivals_sync_hint_follows_the_route_between_identical_histories(app) -> None:
    screen = ArrivalsScreen(Strings("en"), "device-12345678")
    history = [_arrival(0, pending=True)]
    screen.render(history, {}, sync_route_active=False)
    assert screen.sync_hint.isVisibleTo(screen)
    # Signing in changes no arrival, so the list itself is not rebuilt.
    screen.render(history, {}, sync_route_active=True)
    assert not screen.sync_hint.isVisibleTo(screen)


@pytest.mark.parametrize(
    ("mode", "authenticated", "active"),
    [
        ("offline", False, False),
        ("offline", True, False),
        ("centralized", False, False),
        ("centralized", True, True),
        ("iroh", False, True),
    ],
)
def test_sync_route_is_active_only_with_an_account_or_a_room(mode, authenticated, active) -> None:
    window = Mock(replication_mode=mode)
    window.cloud.authenticated = authenticated
    assert MainWindowViewMixin._sync_route_active(window) is active


def test_readme_title_names_every_desktop_platform() -> None:
    title = README.read_text(encoding="utf-8").splitlines()[0]
    assert title == "# Pomodorough for Linux, macOS, and Windows"
