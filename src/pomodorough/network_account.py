from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .network_session import ApiError, Request, SessionState, Text, TokenStorePort
from .secure_store import SecureStoreError, TokenCleanupPendingError
from .storage_revocation import PendingSessionRevocations, credential_api_base


class AccountTokenStorePort(TokenStorePort, Protocol):
    def load_for_revocation(self) -> dict[str, Any] | None: ...

    def clear_if_signed_out(self) -> None: ...


@dataclass(frozen=True)
class AccountDeletionCredentials:
    access_token: str | None = field(repr=False)
    access_expires_at: datetime
    refresh_token: str | None = field(repr=False)
    generation: int


@dataclass(frozen=True)
class LogoutCredentials:
    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)
    access_token_is_fresh: bool
    api_base: str
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)


class SignOutCleanupError(SecureStoreError):
    def __init__(self, credentials: LogoutCredentials, generation: int) -> None:
        super().__init__(
            "Signed out locally. Secure credential cleanup failed and will be retried."
        )
        self.credentials = credentials
        self.generation = generation


@dataclass
class RevocationState:
    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)
    access_token_is_fresh: bool
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)
    acknowledged: bool = False
    api_base: str | None = field(default=None, kw_only=True)
    _durable_credentials: dict[str, Any] | None = field(default=None, init=False, repr=False)


class AccountLifecycle:
    def __init__(
        self,
        api_base: str,
        state: SessionState,
        token_store: AccountTokenStorePort,
        request: Request,
        text: Text,
        now: Callable[[], datetime],
        revocations: PendingSessionRevocations,
    ) -> None:
        self.api_base = api_base
        self.state = state
        self.token_store = token_store
        self.request = request
        self.text = text
        self.now = now
        self.revocations = revocations

    def begin_deletion(
        self,
        confirmation: str,
    ) -> AccountDeletionCredentials | None:
        with self.state.lock:
            if (
                confirmation != "DELETE"
                or not self.state.authenticated
                or self.state.deleting_account
                or self.state.shutting_down
            ):
                return None
            self.state.account_generation += 1
            credentials = AccountDeletionCredentials(
                self.state.access_token,
                self.state.access_expires_at,
                self.state.refresh_token,
                self.state.account_generation,
            )
            self.state.deleting_account = True
            return credentials

    def delete_account(
        self,
        credentials: AccountDeletionCredentials,
        accept_tokens: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        token = self._deletion_access(credentials, accept_tokens)
        try:
            return self._delete_request(token, credentials)
        except ApiError as error:
            if error.status != 401 or token != credentials.access_token:
                raise
            token = self.refresh_deletion_access(credentials, accept_tokens)
            return self._delete_request(token, credentials)

    def _deletion_access(
        self,
        credentials: AccountDeletionCredentials,
        accept_tokens: Callable[[dict[str, Any]], None],
    ) -> str:
        if (
            credentials.access_token
            and credentials.access_expires_at
            > self.now() + timedelta(seconds=30)
        ):
            return credentials.access_token
        return self.refresh_deletion_access(credentials, accept_tokens)

    def _delete_request(
        self, access_token: str, credentials: AccountDeletionCredentials
    ) -> dict[str, Any]:
        with self.state.lock:
            self._assert_deletion_current_locked(credentials)
        return self.request(
            "DELETE",
            f"{self.api_base}/api/v1/account",
            {"confirmation": "DELETE"},
            access_token=access_token,
        )

    def refresh_deletion_access(
        self,
        credentials: AccountDeletionCredentials,
        accept_tokens: Callable[[dict[str, Any]], None],
    ) -> str:
        with self.state.lock:
            self._assert_deletion_current_locked(credentials)
            if not credentials.refresh_token:
                raise ApiError(self.text("network.error.sign_in_required"))
            if credentials.refresh_token != self.state.refresh_token:
                raise ApiError(self.text("network.error.sign_in_cancelled"))
        response = self.request(
            "POST",
            f"{self.api_base}/api/v1/auth/refresh",
            {"refreshToken": credentials.refresh_token},
        )
        with self.state.lock:
            self._assert_deletion_current_locked(credentials)
            if credentials.refresh_token != self.state.refresh_token:
                raise ApiError(self.text("network.error.sign_in_cancelled"))
            accept_tokens(response)
            return self.state.access_token or ""

    def _deletion_current_locked(self, credentials: AccountDeletionCredentials) -> bool:
        return bool(
            credentials.generation == self.state.account_generation
            and not self.state.shutting_down
            and self.state.authenticated
            and self.state.deleting_account
        )

    def _assert_deletion_current_locked(self, credentials: AccountDeletionCredentials) -> None:
        if not self._deletion_current_locked(credentials):
            raise ApiError(self.text("network.error.sign_in_cancelled"))

    def complete_deletion(self, credentials: AccountDeletionCredentials) -> bool:
        with self.state.lock:
            if not self._deletion_current_locked(credentials):
                return False
            self.state.account_generation += 1
            self.state.busy = False
            self.state.deleting_account = False
            self.state.sync_queued = None
            self._clear_store_safely()
            self._clear_session()
            return True

    def fail_deletion(self, credentials: AccountDeletionCredentials) -> bool:
        with self.state.lock:
            if not self._deletion_current_locked(credentials):
                return False
            self.state.deleting_account = False
            return True

    def sign_out(self) -> LogoutCredentials:
        with self.state.lock:
            credentials = self._logout_credentials()
            if credentials.access_token or credentials.refresh_token:
                self.enqueue_revocation(RevocationState(
                    credentials.access_token, credentials.refresh_token,
                    credentials.access_token_is_fresh, credentials.identifier,
                    api_base=credentials.api_base,
                ))
            cleanup_complete = self._clear_sign_out_store()
            self.state.account_generation += 1
            self.state.busy = False
            self.state.deleting_account = False
            self.state.sync_queued = None
            self._clear_session()
            if not cleanup_complete:
                raise SignOutCleanupError(credentials, self.state.account_generation) from None
            return credentials

    def _clear_sign_out_store(self) -> bool:
        try:
            self.token_store.clear()
        except TokenCleanupPendingError:
            return False
        except (OSError, subprocess.SubprocessError):
            raise SecureStoreError(
                "Sign out could not be persisted. Session retained; retry sign out."
            ) from None
        return True

    def retry_sign_out_cleanup(self, generation: int) -> None:
        with self.state.lock:
            if (
                self.state.shutting_down or generation != self.state.account_generation
                or self.state.authenticated or self.state.access_token or self.state.refresh_token
            ):
                return
            try:
                self.token_store.clear_if_signed_out()
            except (OSError, subprocess.SubprocessError):
                raise SecureStoreError(
                    "Signed out locally. Secure credential cleanup failed and will be retried."
                ) from None

    def _logout_credentials(self) -> LogoutCredentials:
        token, refresh = self.state.access_token, self.state.refresh_token
        if token or refresh:
            fresh = bool(token and self.state.access_expires_at > self.now() + timedelta(seconds=30))
            return LogoutCredentials(token, refresh, fresh, self.api_base)
        stored = self.token_store.load_for_revocation()
        refresh_token = stored.get("refreshToken") if stored else None
        if isinstance(refresh_token, str) and refresh_token:
            return LogoutCredentials(None, refresh_token, False, credential_api_base(stored["apiBase"]))
        return LogoutCredentials(None, None, False, self.api_base)

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
        self.state.access_expires_at = datetime.min.replace(tzinfo=UTC)
        self.state.authenticated = False

    def revocation(
        self,
        access_token: str | None,
        refresh_token: str | None,
        access_token_is_fresh: bool,
        identifier: str | None = None,
        api_base: str | None = None,
    ) -> RevocationState:
        revocation = RevocationState(
            access_token,
            refresh_token,
            access_token_is_fresh,
            identifier or uuid.uuid4().hex,
            api_base=credential_api_base(self.api_base if api_base is None else api_base),
        )
        if identifier is None:
            self.enqueue_revocation(revocation)
        return revocation

    def enqueue_revocation(self, revocation: RevocationState) -> None:
        self.revocations.enqueue(
            self._revocation_api(revocation), revocation.identifier,
            self._revocation_credentials(revocation),
        )

    def revoke(self, revocation: RevocationState) -> None:
        """Enqueue legacy unbound credentials once; bound jobs only replay."""
        if revocation.api_base is None:
            self.enqueue_revocation(revocation)
        api_base = self._revocation_api(revocation)
        with self.revocations.claim(api_base, revocation.identifier):
            current = self.revocations.load(api_base).get(revocation.identifier)
            if current is None:
                revocation.acknowledged = True
                return
            self._revalidate_revocation(revocation, current)
            if not revocation.acknowledged:
                self._revoke_session(revocation)
                revocation.acknowledged = True
                self._save_revocation(revocation)
            self.revocations.acknowledge(api_base, revocation.identifier)

    def _revalidate_revocation(
        self, revocation: RevocationState, current: dict[str, Any]
    ) -> None:
        if revocation._durable_credentials == current:
            self._save_revocation(revocation)
            return
        revocation.access_token = current["accessToken"]
        revocation.refresh_token = current["refreshToken"]
        revocation.access_token_is_fresh = current["accessTokenIsFresh"]
        revocation.acknowledged = current["acknowledged"]
        revocation._durable_credentials = current

    def pending_revocations(self) -> list[RevocationState]:
        return [
            RevocationState(
                credentials["accessToken"], credentials["refreshToken"],
                credentials["accessTokenIsFresh"], identifier,
                credentials["acknowledged"],
                api_base=api_base,
            )
            for api_base, pending in self.revocations.load_all(self.api_base).items()
            for identifier, credentials in pending.items()
        ]

    def _save_revocation(self, revocation: RevocationState) -> None:
        credentials = self._revocation_credentials(revocation)
        self.revocations.update(
            self._revocation_api(revocation), revocation.identifier, credentials,
        )
        revocation._durable_credentials = credentials

    @staticmethod
    def _revocation_credentials(revocation: RevocationState) -> dict[str, Any]:
        return {
            "accessToken": revocation.access_token,
            "refreshToken": revocation.refresh_token,
            "accessTokenIsFresh": revocation.access_token_is_fresh,
            "acknowledged": revocation.acknowledged,
        }

    def _revocation_api(self, revocation: RevocationState) -> str:
        if revocation.api_base is None:
            revocation.api_base = self.api_base
        return credential_api_base(revocation.api_base)

    def _revoke_session(self, revocation: RevocationState) -> None:
        token = self._revocation_access(revocation)
        try:
            self._logout_request(revocation.api_base, token)
        except ApiError as error:
            if error.status != 401 or not revocation.refresh_token:
                raise
            revocation.access_token_is_fresh = False
            self._logout_request(revocation.api_base, self.refresh_revocation_access(revocation))

    def _revocation_access(self, revocation: RevocationState) -> str:
        if (
            revocation.access_token_is_fresh
            and isinstance(revocation.access_token, str)
            and revocation.access_token
        ):
            return revocation.access_token
        return self.refresh_revocation_access(revocation)

    def _logout_request(self, api_base: str, access_token: str) -> None:
        self.request(
            "POST",
            f"{api_base}/api/v1/auth/logout",
            {},
            access_token=access_token,
        )

    def refresh_revocation_access(self, revocation: RevocationState) -> str:
        api_base = self._revocation_api(revocation)
        captured_refresh = revocation.refresh_token
        if not isinstance(captured_refresh, str) or not captured_refresh:
            raise ApiError(self.text("network.error.sign_in_required"))
        response = self.request(
            "POST",
            f"{api_base}/api/v1/auth/refresh",
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
        self._save_revocation(revocation)
        return refreshed_access
