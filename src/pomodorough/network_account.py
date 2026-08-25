from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .network_session import ApiError, Request, SessionState, Text, TokenStorePort


@dataclass(frozen=True)
class AccountDeletionCredentials:
    access_token: str | None
    access_expires_at: datetime
    refresh_token: str | None


@dataclass(frozen=True)
class LogoutCredentials:
    access_token: str | None
    refresh_token: str | None
    access_token_is_fresh: bool


@dataclass
class RevocationState:
    access_token: str | None
    refresh_token: str | None
    access_token_is_fresh: bool


class AccountLifecycle:
    def __init__(
        self,
        api_base: str,
        state: SessionState,
        token_store: TokenStorePort,
        request: Request,
        text: Text,
        now: Callable[[], datetime],
    ) -> None:
        self.api_base = api_base
        self.state = state
        self.token_store = token_store
        self.request = request
        self.text = text
        self.now = now

    def begin_deletion(
        self,
        confirmation: str,
    ) -> AccountDeletionCredentials | None:
        with self.state.lock:
            if (
                confirmation != "DELETE"
                or not self.state.authenticated
                or self.state.deleting_account
            ):
                return None
            credentials = AccountDeletionCredentials(
                self.state.access_token,
                self.state.access_expires_at,
                self.state.refresh_token,
            )
            self.state.account_generation += 1
            self.state.deleting_account = True
            return credentials

    def delete_account(
        self,
        credentials: AccountDeletionCredentials,
    ) -> dict[str, Any]:
        token = self._deletion_access(credentials)
        try:
            return self._delete_request(token)
        except ApiError as error:
            if error.status != 401 or token != credentials.access_token:
                raise
            return self._delete_request(self.refresh_deletion_access(credentials))

    def _deletion_access(self, credentials: AccountDeletionCredentials) -> str:
        if (
            credentials.access_token
            and credentials.access_expires_at
            > self.now() + timedelta(seconds=30)
        ):
            return credentials.access_token
        return self.refresh_deletion_access(credentials)

    def _delete_request(self, access_token: str) -> dict[str, Any]:
        return self.request(
            "DELETE",
            f"{self.api_base}/api/v1/account",
            {"confirmation": "DELETE"},
            access_token=access_token,
        )

    def refresh_deletion_access(
        self,
        credentials: AccountDeletionCredentials,
    ) -> str:
        if not credentials.refresh_token:
            raise ApiError(self.text("network.error.sign_in_required"))
        response = self.request(
            "POST",
            f"{self.api_base}/api/v1/auth/refresh",
            {"refreshToken": credentials.refresh_token},
        )
        refreshed = response.get("accessToken")
        if not isinstance(refreshed, str) or not refreshed.strip():
            raise ApiError(self.text("network.error.invalid_token"))
        return refreshed

    def complete_deletion(self) -> None:
        self.state.account_generation += 1
        self.state.busy = False
        self.state.deleting_account = False
        self.state.sync_queued = None
        self._clear_store_safely()
        self._clear_session()

    def fail_deletion(self) -> None:
        self.state.deleting_account = False

    def sign_out(self) -> LogoutCredentials:
        with self.state.lock:
            token = self.state.access_token
            refresh_token = self.state.refresh_token
            credentials = LogoutCredentials(
                token,
                refresh_token,
                bool(
                    token
                    and self.state.access_expires_at
                    > self.now() + timedelta(seconds=30)
                ),
            )
            self.state.account_generation += 1
            self.state.busy = False
            self.state.deleting_account = False
            self.state.sync_queued = None
            self.token_store.clear()
            self._clear_session()
            return credentials

    def expire_session(self) -> None:
        self._clear_store_safely()
        self._clear_session()

    def _clear_store_safely(self) -> None:
        try:
            self.token_store.clear()
        except (OSError, subprocess.SubprocessError):
            pass

    def _clear_session(self) -> None:
        self.state.access_token = None
        self.state.refresh_token = None
        self.state.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.state.authenticated = False

    @staticmethod
    def revocation(
        access_token: str | None,
        refresh_token: str | None,
        access_token_is_fresh: bool,
    ) -> RevocationState:
        return RevocationState(
            access_token,
            refresh_token,
            access_token_is_fresh,
        )

    def revoke(self, revocation: RevocationState) -> None:
        token = self._revocation_access(revocation)
        try:
            self._logout_request(token)
        except ApiError as error:
            if error.status != 401 or not revocation.refresh_token:
                raise
            revocation.access_token_is_fresh = False
            self._logout_request(self.refresh_revocation_access(revocation))

    def _revocation_access(self, revocation: RevocationState) -> str:
        if (
            revocation.access_token_is_fresh
            and isinstance(revocation.access_token, str)
            and revocation.access_token
        ):
            return revocation.access_token
        return self.refresh_revocation_access(revocation)

    def _logout_request(self, access_token: str) -> None:
        self.request(
            "POST",
            f"{self.api_base}/api/v1/auth/logout",
            {},
            access_token=access_token,
        )

    def refresh_revocation_access(self, revocation: RevocationState) -> str:
        captured_refresh = revocation.refresh_token
        if not isinstance(captured_refresh, str) or not captured_refresh:
            raise ApiError(self.text("network.error.sign_in_required"))
        response = self.request(
            "POST",
            f"{self.api_base}/api/v1/auth/refresh",
            {"refreshToken": captured_refresh},
        )
        refreshed_access = response.get("accessToken")
        if not isinstance(refreshed_access, str) or not refreshed_access.strip():
            raise ApiError(self.text("network.error.invalid_token"))
        rotated_refresh = response.get("refreshToken")
        if isinstance(rotated_refresh, str) and rotated_refresh.strip():
            revocation.refresh_token = rotated_refresh
        revocation.access_token = refreshed_access
        revocation.access_token_is_fresh = True
        return refreshed_access
