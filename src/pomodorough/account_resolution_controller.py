from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QWidget

from .controller_outcomes import (
    ActivatePersistedResolution,
    ControllerOutcome,
    EmitNotice,
    LoadState,
    MaybeAutoStartBreak,
    Render,
    RenderNetwork,
    SchedulePendingAutoBreak,
    SetAccountState,
    ShowStatus,
    Synchronize,
    done,
    returning,
)


@dataclass(frozen=True, slots=True)
class AccountResolutionContext:
    store: Any
    cloud: Any
    strings: Any
    user: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class AccountPresentation:
    authenticated: bool
    text: str
    tooltip: str
    accessible_name: str


@dataclass(frozen=True, slots=True)
class AccountResolutionPorts:
    context: Callable[[], AccountResolutionContext]
    apply_outcome: Callable[[ControllerOutcome[Any]], None]
    response_timing: Callable[[dict[str, Any]], dict[str, int | None]]
    dialog_parent: Callable[[], QWidget]
    present_account: Callable[[AccountPresentation], None]
    prompt_history_resolution: Callable[[], str | None]
    confirm_history_resolution: Callable[[str], bool]
    choose_resolution_account_action: Callable[[], str | None]
    choose_account_switch_action: Callable[[], str | None]
    continue_history_resolution: Callable[[], None]
    bootstrap_ready: Callable[[dict[str, Any]], None]
    signed_in: Callable[[dict[str, Any]], None]
    clear_sync_request: Callable[[], None]


class AccountResolutionController:
    """Owns account switching and persisted history-resolution lifecycle."""

    def __init__(self, ports: AccountResolutionPorts) -> None:
        self._ports = ports
        self.account_synced = False
        self.history_resolution_active = False
        self.resolution_user: dict[str, Any] | None = None
        self.resolution_phase: str | None = None
        self.resolution_preview: dict[str, Any] | None = None
        self.resolution_request_id: str | None = None
        self.resolution_retry_paused = False
        self.resolution_retry_scheduled = False
        self.resolution_corruption: str | None = None
        self.account_switch_user: dict[str, Any] | None = None

    def _context(self) -> AccountResolutionContext:
        return self._ports.context()

    def activate_persisted_resolution(self) -> ControllerOutcome[bool]:
        context = self._context()
        try:
            pending = context.store.pending_resolution()
        except ValueError as error:
            self.resolution_corruption = str(error)
            self.history_resolution_active = True
            self.resolution_user = context.user
            self.resolution_phase = None
            self.resolution_preview = None
            self.resolution_request_id = None
            self.resolution_retry_paused = True
            return returning(True)
        if pending is None:
            return returning(False)
        self.history_resolution_active = True
        self.resolution_user = pending["owner"]
        self.resolution_phase = "resolve"
        self.resolution_preview = None
        self.resolution_request_id = None
        self.resolution_retry_paused = False
        return returning(True)

    def schedule_resolution_retry(self) -> ControllerOutcome[None]:
        if self.resolution_retry_scheduled:
            return done()
        self.resolution_retry_scheduled = True
        QTimer.singleShot(100, self._retry_history_resolution_callback)
        return done()

    def _retry_history_resolution_callback(self) -> None:
        self.resolution_retry_scheduled = False
        self._ports.continue_history_resolution()

    def retry_history_resolution(self) -> ControllerOutcome[None]:
        self.resolution_retry_scheduled = False
        self._ports.continue_history_resolution()
        return done()

    def resume_history_resolution(self) -> ControllerOutcome[None]:
        if not self.history_resolution_active:
            return done()
        self.resolution_retry_paused = False
        if self.resolution_phase == "choice" and self.resolution_preview is not None:
            self._ports.bootstrap_ready(self.resolution_preview)
            return done()
        return self.continue_history_resolution()

    def continue_history_resolution(self) -> ControllerOutcome[None]:
        context = self._context()
        if (
            not self.history_resolution_active
            or self.resolution_retry_paused
            or not context.cloud.authenticated
            or self.resolution_user is None
            or self.account_switch_user is not None
        ):
            return done()
        if context.cloud.busy:
            return self.schedule_resolution_retry()
        if self.resolution_phase == "resolve":
            pending = context.store.pending_resolution(
                str(self.resolution_user.get("id", ""))
            )
            if pending is None:
                self.resolution_phase = "preview"
            else:
                request = pending["request"]
                self.resolution_request_id = request.get("requestId")
                context.cloud.resolve_bootstrap(request)
                return done()
        if self.resolution_phase == "preview":
            context.cloud.preview_bootstrap()
        return done()

    def bootstrap_ready(self, response: dict[str, Any]) -> ControllerOutcome[None]:
        if not self.history_resolution_active or self.resolution_user is None:
            return done()
        context = self._context()
        self.resolution_phase = "choice"
        try:
            plan = context.store.bootstrap_resolution_plan(
                response,
                **self._ports.response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self.resolution_phase = "preview"
            self.resolution_preview = None
            self.resolution_retry_paused = True
            return done(EmitNotice(str(error)))
        self.resolution_preview = response
        strategy = plan["strategy"]
        if strategy is None:
            strategy = self._ports.prompt_history_resolution()
            if strategy is None or not self._ports.confirm_history_resolution(strategy):
                self.resolution_retry_paused = True
                return done()
        try:
            context.store.prepare_resolution(
                self.resolution_user,
                int(plan["expectedRevision"]),
                strategy,
            )
        except (KeyError, TypeError, ValueError) as error:
            self.resolution_retry_paused = True
            return done(EmitNotice(str(error)))
        self.resolution_phase = "resolve"
        return self.continue_history_resolution()

    def prompt_history_resolution(self) -> ControllerOutcome[str | None]:
        strings = self._context().strings
        dialog = QMessageBox(self._ports.dialog_parent())
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(strings.text("resolution.title"))
        dialog.setText(strings.text("resolution.question"))
        dialog.setInformativeText(strings.text("resolution.detail"))
        keep_local = dialog.addButton(
            strings.text("resolution.keep_local"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        keep_remote = dialog.addButton(
            strings.text("resolution.keep_remote"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        keep_both = dialog.addButton(
            strings.text("resolution.keep_both"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return returning(
            {
                keep_local: "replace_remote",
                keep_remote: "keep_remote",
                keep_both: "merge",
            }.get(dialog.clickedButton())
        )

    def confirm_history_resolution(self, strategy: str) -> ControllerOutcome[bool]:
        strings = self._context().strings
        messages = {
            "replace_remote": (
                strings.text("resolution.confirm_local"),
                strings.text("resolution.confirm_local_detail"),
            ),
            "keep_remote": (
                strings.text("resolution.confirm_remote"),
                strings.text("resolution.confirm_remote_detail"),
            ),
            "merge": (
                strings.text("resolution.confirm_both"),
                strings.text("resolution.confirm_both_detail"),
            ),
        }
        title, message = messages[strategy]
        answer = QMessageBox.warning(
            self._ports.dialog_parent(),
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return returning(answer == QMessageBox.StandardButton.Yes)

    def apply_resolution(self, response: dict[str, Any]) -> ControllerOutcome[None]:
        if not self.history_resolution_active or self.resolution_user is None:
            return done()
        context = self._context()
        try:
            notices = context.store.apply_resolution(
                response,
                self.resolution_user,
                self.resolution_request_id,
                **self._ports.response_timing(response),
            )
        except (KeyError, TypeError, ValueError) as error:
            self.resolution_retry_paused = True
            return done(EmitNotice(str(error)))
        self._clear_history_resolution_value()
        self._ports.apply_outcome(
            done(
                LoadState(),
                Render(),
                MaybeAutoStartBreak(sync=False, allow_busy=True),
            )
        )
        context = self._context()
        has_pending = context.store.has_sendable_sync_operations()
        effects: list[Any] = [SetAccountState(not has_pending)]
        if has_pending:
            effects.append(Synchronize())
        if notices:
            effects.append(
                EmitNotice(
                    context.strings.text(
                        "resolution.history_conflict", detail="; ".join(notices)
                    )
                )
            )
        return done(*effects)

    def bootstrap_conflict(self, details: dict[str, Any]) -> ControllerOutcome[None]:
        if not self.history_resolution_active or self.resolution_user is None:
            return done()
        context = self._context()
        user_id = self.resolution_user.get("id")
        request_id = self.resolution_request_id
        if (
            not isinstance(user_id, str)
            or not isinstance(request_id, str)
            or not context.store.discard_pending_resolution(user_id, request_id)
        ):
            self.resolution_retry_paused = True
            return done(EmitNotice(context.strings.text("resolution.discard_failed")))
        self.resolution_phase = "preview"
        self.resolution_preview = None
        self.resolution_request_id = None
        self.resolution_retry_paused = False
        message = details.get("message") if isinstance(details, dict) else None
        self._ports.apply_outcome(
            done(
                ShowStatus(
                    (message or context.strings.text("resolution.changed"))
                    + context.strings.text("resolution.refreshing"),
                    10_000,
                )
            )
        )
        self._ports.continue_history_resolution()
        return done()

    def signed_in(self, user: dict[str, Any]) -> ControllerOutcome[None]:
        context = self._context()
        try:
            pending = context.store.pending_resolution()
        except ValueError as error:
            return self.handle_resolution_corruption(user, error)
        owner = pending["owner"] if pending is not None else context.user
        owner_id = owner.get("id") if isinstance(owner, dict) else None
        if owner_id and owner_id != user.get("id"):
            return self.quarantine_account_switch(user, owner, pending is not None)
        self.account_switch_user = None
        if context.user is not None:
            return self.accept_existing_account(user)
        phase = "resolve" if pending is not None else "preview"
        self._begin_history_resolution_value(user, phase, paused=False)
        self._ports.apply_outcome(done(Render(), SetAccountState(False)))
        return self.continue_history_resolution()

    def begin_history_resolution(
        self, user: dict[str, Any], phase: str | None, *, paused: bool
    ) -> ControllerOutcome[None]:
        self._begin_history_resolution_value(user, phase, paused=paused)
        return done()

    def _begin_history_resolution_value(
        self, user: dict[str, Any], phase: str | None, *, paused: bool
    ) -> None:
        self.history_resolution_active = True
        self.resolution_user = user
        self.resolution_phase = phase
        self.resolution_preview = None
        self.resolution_request_id = None
        self.resolution_retry_paused = paused

    def clear_history_resolution(self) -> ControllerOutcome[None]:
        self._clear_history_resolution_value()
        return done()

    def _clear_history_resolution_value(self) -> None:
        self.history_resolution_active = False
        self.resolution_user = None
        self.resolution_phase = None
        self.resolution_preview = None
        self.resolution_request_id = None
        self.resolution_retry_paused = False

    def handle_resolution_corruption(
        self, user: dict[str, Any], error: ValueError
    ) -> ControllerOutcome[None]:
        context = self._context()
        self.resolution_corruption = str(error)
        self._begin_history_resolution_value(context.user or user, None, paused=True)
        self._ports.clear_sync_request()
        return done(Render(), SetAccountState(False), EmitNotice(str(error)))

    def quarantine_account_switch(
        self,
        user: dict[str, Any],
        owner: dict[str, Any],
        has_pending_resolution: bool,
    ) -> ControllerOutcome[None]:
        self.account_switch_user = user
        phase = "resolve" if has_pending_resolution else None
        self._begin_history_resolution_value(owner, phase, paused=True)
        self._ports.clear_sync_request()
        return done(Render(), SetAccountState(False))

    def accept_existing_account(self, user: dict[str, Any]) -> ControllerOutcome[None]:
        self._context().store.set_user(user)
        self._clear_history_resolution_value()
        return done(LoadState(), Render(), SetAccountState(False), Synchronize())

    def signed_out(self) -> ControllerOutcome[None]:
        context = self._context()
        if self.account_switch_user is None:
            context.store.reset_account_data()
        self.account_switch_user = None
        self._clear_history_resolution_value()
        self._ports.clear_sync_request()
        return done(
            LoadState(),
            ActivatePersistedResolution(),
            SetAccountState(False),
            Render(),
        )

    def session_expired(self) -> ControllerOutcome[None]:
        self.account_switch_user = None
        self._clear_history_resolution_value()
        self._ports.clear_sync_request()
        return done(
            LoadState(),
            ActivatePersistedResolution(),
            SetAccountState(False),
            Render(),
            SchedulePendingAutoBreak(),
        )

    def set_account_state(self, synced: bool) -> ControllerOutcome[None]:
        context = self._context()
        self.account_synced = synced and context.cloud.authenticated
        authenticated = context.cloud.authenticated
        if not authenticated:
            presentation = AccountPresentation(
                authenticated=False,
                text=context.strings.text("account.sign_in"),
                tooltip=context.strings.text("account.sign_in_google"),
                accessible_name=context.strings.text("account.sign_in_google"),
            )
        elif self.account_switch_user is not None:
            presentation = AccountPresentation(
                authenticated=True,
                text="!",
                tooltip=context.strings.text("account.switch_tooltip"),
                accessible_name=context.strings.text("account.switch_required"),
            )
        elif self.history_resolution_active:
            presentation = AccountPresentation(
                authenticated=True,
                text="!",
                tooltip=context.strings.text("account.resolution_tooltip"),
                accessible_name=context.strings.text("account.resolution_required"),
            )
        elif self.account_synced:
            presentation = AccountPresentation(
                authenticated=True,
                text="✓",
                tooltip=context.strings.text("account.synced_tooltip"),
                accessible_name=context.strings.text("account.synced"),
            )
        else:
            presentation = AccountPresentation(
                authenticated=True,
                text="…",
                tooltip=context.strings.text("account.pending_tooltip"),
                accessible_name=context.strings.text("account.pending"),
            )
        self._ports.present_account(presentation)
        return done()

    def delete_account_action(self) -> ControllerOutcome[None]:
        context = self._context()
        if not context.cloud.authenticated or context.cloud.deleting_account:
            return done()
        confirmation, accepted = QInputDialog.getText(
            self._ports.dialog_parent(),
            context.strings.text("account.delete_prompt_title"),
            context.strings.text("account.delete_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not accepted:
            return done()
        if confirmation != "DELETE":
            return done(
                ShowStatus(context.strings.text("account.delete_mismatch"), 10_000)
            )
        context.cloud.delete_account(confirmation)
        return done(RenderNetwork())

    def account_deleted(self) -> ControllerOutcome[None]:
        outcome = self.signed_out()
        return ControllerOutcome(
            None,
            outcome.effects
            + (
                ShowStatus(
                    self._context().strings.text("account.delete_succeeded"), 10_000
                ),
            ),
        )

    def account_deletion_failed(self, error: str) -> ControllerOutcome[None]:
        return done(
            Render(),
            ShowStatus(
                self._context().strings.text("account.delete_failed", error=error),
                15_000,
            ),
        )

    def account_action(self) -> ControllerOutcome[None]:
        context = self._context()
        if context.cloud.authenticated:
            if self.account_switch_user is not None:
                action = self._ports.choose_account_switch_action()
                if action == "switch":
                    user = self.account_switch_user
                    context.store.reset_account_data()
                    self.account_switch_user = None
                    self._ports.apply_outcome(done(LoadState()))
                    self._ports.signed_in(user)
                elif action == "sign_out":
                    context.cloud.logout()
                return done()
            if self.history_resolution_active:
                action = self._ports.choose_resolution_account_action()
                if action == "continue":
                    return self.resume_history_resolution()
                if action == "sign_out":
                    context.cloud.logout()
                return done()
            state = context.store.load()
            queued_changes = sum(
                len(state[key])
                for key in (
                    "pending",
                    "pendingTasks",
                    "pendingDurations",
                    "pendingAutoStarts",
                    "pendingSelectedTasks",
                )
            )
            queue_label = context.strings.plural("queue.changes", queued_changes)
            answer = QMessageBox.question(
                self._ports.dialog_parent(),
                context.strings.text("account.signout_title"),
                context.strings.text("account.signout_detail", queue=queue_label),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                context.cloud.logout()
        else:
            context.cloud.login()
        return done()

    def choose_resolution_account_action(
        self,
    ) -> ControllerOutcome[str | None]:
        strings = self._context().strings
        dialog = QMessageBox(self._ports.dialog_parent())
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(strings.text("account.resolve_title"))
        dialog.setText(strings.text("account.resolve_question"))
        dialog.setInformativeText(strings.text("account.resolve_detail"))
        resume = dialog.addButton(
            strings.text("account.continue_resolution"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        sign_out = dialog.addButton(
            strings.text("account.sign_out"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return returning(
            {resume: "continue", sign_out: "sign_out"}.get(dialog.clickedButton())
        )

    def choose_account_switch_action(self) -> ControllerOutcome[str | None]:
        strings = self._context().strings
        dialog = QMessageBox(self._ports.dialog_parent())
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(strings.text("account.different_title"))
        dialog.setText(strings.text("account.different_question"))
        dialog.setInformativeText(strings.text("account.different_detail"))
        switch = dialog.addButton(
            strings.text("account.clear_switch"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        sign_out = dialog.addButton(
            strings.text("account.sign_out"), QMessageBox.ButtonRole.RejectRole
        )
        cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return returning(
            {switch: "switch", sign_out: "sign_out"}.get(dialog.clickedButton())
        )
