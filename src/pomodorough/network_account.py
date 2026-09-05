from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .network_session import ApiError, Request, SessionState, Text, TokenStorePort
from .secure_store import SecureStoreError, TokenCleanupPendingError, token_store_lock
from .storage_revocation import PendingSessionRevocations, credential_api_base

_CLEARED_DELETION_CLEANUP = {"version": 1, "cleanupState": "cleared"}
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8
_DELETION_CLEANUP_LIMIT = 64 * 1024


class _LocalDocumentTooLarge(Exception):
    pass


def _read_capped_text(path: Path, limit: int) -> str:
    """Read a small local JSON document with an oversize rejection."""
    if path.stat().st_size > limit:
        raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > limit:
        raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
    return text


def _platform_name(filesystem: Any = os) -> str:
    return str(getattr(filesystem, "name", ""))


def _load_windows_kernel32() -> Any:
    import ctypes

    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise SecureStoreError(
            "Windows durable file replacement is unsupported."
        ) from error


def _last_windows_error() -> int:
    import ctypes

    return ctypes.get_last_error()


def _replace_windows_write_through(source: Path, destination: Path) -> None:
    import ctypes

    move_file = _load_windows_kernel32().MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    flags = _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
    if move_file(str(source.absolute()), str(destination.absolute()), flags):
        return
    error_code = _last_windows_error()
    raise OSError(
        error_code,
        "Atomic write-through file replacement failed.",
        str(destination),
    )


def _replace_file_for_durable_commit(
    source: Path, destination: Path, filesystem: Any = os
) -> None:
    platform = _platform_name(filesystem)
    if platform == "nt":
        _replace_windows_write_through(source, destination)
        return
    if platform != "posix":
        raise SecureStoreError("Durable file replacement is unsupported.")
    filesystem.replace(source, destination)


def _sync_replaced_file_directory(path: Path, filesystem: Any = os) -> None:
    platform = _platform_name(filesystem)
    if platform == "nt":
        return
    if platform != "posix":
        raise SecureStoreError("Directory durability is unsupported.")
    if not hasattr(filesystem, "O_DIRECTORY"):
        if filesystem is not os:
            _sync_replaced_file_directory(path)
            return
        raise SecureStoreError("Directory durability is unsupported.")
    descriptor = filesystem.open(
        path.parent, filesystem.O_RDONLY | filesystem.O_DIRECTORY
    )
    try:
        filesystem.fsync(descriptor)
    finally:
        filesystem.close(descriptor)


def _account_deletion_cleanup_blocks_authentication(path: Path) -> bool:
    try:
        document = json.loads(_read_capped_text(path, _DELETION_CLEANUP_LIMIT))
    except FileNotFoundError:
        return False
    except _LocalDocumentTooLarge:
        raise SecureStoreError(
            "Account deletion cleanup obligation is unreadable or malformed."
        ) from None
    except (OSError, ValueError, UnicodeError):
        raise SecureStoreError(
            "Account deletion cleanup obligation is unreadable or malformed."
        ) from None
    if document == _CLEARED_DELETION_CLEANUP:
        return False
    if not isinstance(document, dict):
        raise SecureStoreError("Account deletion cleanup obligation is malformed.")
    return True


@contextmanager
def _account_deletion_cleanup_lock(path: Path) -> Iterator[None]:
    acquired = False
    try:
        with token_store_lock(None, "account-deletion-cleanup", path):
            acquired = True
            yield
    except ImportError as error:
        raise SecureStoreError(
            "Account deletion cleanup locking is unsupported."
        ) from error
    except OSError as error:
        if not acquired:
            raise SecureStoreError(
                "Account deletion cleanup lock is unavailable."
            ) from error
        raise


class AccountTokenStorePort(TokenStorePort, Protocol):
    def load_for_revocation(self) -> dict[str, Any] | None: ...

    def clear_if_signed_out(self) -> None: ...

    def account_deletion_cleanup_path(self) -> Path: ...

    def account_deletion_credential_identity(
        self,
    ) -> AbstractContextManager[_DeletionCredentialIdentity | None]: ...

    def account_deletion_credentials_locked(
        self,
    ) -> AbstractContextManager[None]: ...

    def account_deletion_credential_identity_locked(
        self,
    ) -> _DeletionCredentialIdentity | None: ...

    def account_deletion_confirmed_generation_locked(
        self,
    ) -> int | None: ...

    def confirm_account_deletion_locked(
        self,
        api_base: str,
        generation: int,
        identity: _DeletionCredentialIdentity,
    ) -> bool: ...

    def account_deletion_refresh(
        self, identity: _DeletionCredentialIdentity
    ) -> AbstractContextManager[None]: ...

    def clear_account_deletion_credentials(
        self,
        api_base: str,
        identity: _DeletionCredentialIdentity,
    ) -> bool: ...

    def clear_account_deletion_credentials_locked(
        self,
        api_base: str,
        identity: _DeletionCredentialIdentity,
    ) -> bool: ...


@dataclass(frozen=True)
class _DeletionCredentialIdentity:
    api_base: str
    access_token_hash: str
    refresh_token_hash: str

    @classmethod
    def from_tokens(
        cls, api_base: str, access_token: str, refresh_token: str
    ) -> _DeletionCredentialIdentity:
        return cls(
            credential_api_base(api_base),
            AccountLifecycle._token_hash(access_token),
            AccountLifecycle._token_hash(refresh_token),
        )

    def matches(self, other: _DeletionCredentialIdentity) -> bool:
        return bool(
            self.api_base == other.api_base
            and hmac.compare_digest(
                self.access_token_hash, other.access_token_hash
            )
            and hmac.compare_digest(
                self.refresh_token_hash, other.refresh_token_hash
            )
        )

    def matches_session(
        self,
        api_base: str,
        access_token: str | None,
        refresh_token: str | None,
    ) -> bool:
        if access_token is None or credential_api_base(api_base) != self.api_base:
            return False
        if not hmac.compare_digest(
            AccountLifecycle._token_hash(access_token), self.access_token_hash
        ):
            return False
        return refresh_token is None or hmac.compare_digest(
            AccountLifecycle._token_hash(refresh_token), self.refresh_token_hash
        )


@dataclass
class AccountDeletionCredentials:
    access_token: str | None = field(repr=False)
    access_expires_at: datetime
    refresh_token: str | None = field(repr=False)
    generation: int
    identity: _DeletionCredentialIdentity | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _DeletionCleanup:
    api_base: str
    generation: int
    identity: _DeletionCredentialIdentity

    @property
    def refresh_token_hash(self) -> str:
        return self.identity.refresh_token_hash

    @property
    def access_token_hash(self) -> str:
        return self.identity.access_token_hash

    @property
    def bound_credential_api_base(self) -> str:
        return self.identity.api_base

    def document(self) -> dict[str, Any]:
        return {
            "version": 2,
            "cleanupState": "pending",
            "apiBase": self.api_base,
            "generation": self.generation,
            "credentialApiBase": self.identity.api_base,
            "accessTokenHash": self.identity.access_token_hash,
            "refreshTokenHash": self.identity.refresh_token_hash,
        }


@dataclass(frozen=True)
class _LegacyDeletionCleanup:
    api_base: str
    generation: int
    refresh_token_hash: str | None


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
        self._pending_deletion_cleanup: set[int] = set()

    def begin_deletion(
        self,
        confirmation: str,
    ) -> AccountDeletionCredentials | None:
        with self.state.lock:
            self._retry_deletion_cleanup_locked()
            if (
                confirmation != "DELETE"
                or not self.state.authenticated
                or self.state.deleting_account
                or self.state.shutting_down
            ):
                return None
            identity = self._capture_deletion_identity_locked()
            self.state.account_generation += 1
            credentials = AccountDeletionCredentials(
                self.state.access_token,
                self.state.access_expires_at,
                self.state.refresh_token,
                self.state.account_generation,
                identity,
            )
            self.state.deleting_account = True
            return credentials

    def _capture_deletion_identity_locked(self) -> _DeletionCredentialIdentity:
        with self.token_store.account_deletion_credential_identity() as identity:
            if identity is not None and identity.matches_session(
                self.api_base,
                self.state.access_token,
                self.state.refresh_token,
            ):
                return identity
        raise ApiError(self.text("network.error.sign_in_cancelled"))

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
            cleanup = self._prepare_deletion_cleanup_locked(credentials)
        try:
            response = self.request(
                "DELETE",
                f"{self.api_base}/api/v1/account",
                {"confirmation": "DELETE"},
                access_token=access_token,
            )
        except ApiError as error:
            self._handle_deletion_request_error(error, credentials, cleanup)
            raise
        except Exception:
            self._secure_uncertain_deletion(credentials, cleanup)
            raise
        self._finish_remote_deletion(credentials, cleanup)
        return response

    def _prepare_deletion_cleanup_locked(
        self, credentials: AccountDeletionCredentials
    ) -> _DeletionCleanup:
        expected = credentials.identity
        if expected is None:
            raise ApiError(self.text("network.error.sign_in_cancelled"))
        path = self._deletion_cleanup_path()
        try:
            with (
                _account_deletion_cleanup_lock(path),
                self.token_store.account_deletion_credential_identity() as identity,
            ):
                if identity is None or not expected.matches(identity):
                    raise ApiError(self.text("network.error.sign_in_cancelled"))
                cleanup = _DeletionCleanup(self.api_base, credentials.generation, expected)
                self._write_deletion_cleanup_locked(path, cleanup)
        except (OSError, subprocess.SubprocessError):
            if self._deletion_cleanup_exists_safely():
                self._pending_deletion_cleanup.add(credentials.generation)
            raise
        return cleanup

    def _handle_deletion_request_error(
        self,
        error: ApiError,
        credentials: AccountDeletionCredentials,
        cleanup: _DeletionCleanup | None,
    ) -> None:
        if error.status is None:
            self._secure_uncertain_deletion(credentials, cleanup)
            return
        try:
            self._remove_deletion_cleanup(cleanup)
        except (OSError, subprocess.SubprocessError):
            self._pending_deletion_cleanup.add(credentials.generation)
            raise TokenCleanupPendingError(
                "Account deletion failed and local cleanup is pending."
            ) from None

    def _secure_uncertain_deletion(
        self,
        credentials: AccountDeletionCredentials,
        cleanup: _DeletionCleanup | None,
    ) -> None:
        with self.state.lock:
            if not self._deletion_current_locked(credentials):
                self._settle_stale_deletion_cleanup_locked(credentials, cleanup)
                return
            try:
                resolved = self._resolve_deletion_cleanup_locked(cleanup)
            except (OSError, subprocess.SubprocessError):
                resolved = False
            if cleanup is not None or not resolved or self._deletion_cleanup_exists_safely():
                self._pending_deletion_cleanup.add(credentials.generation)

    def _finish_remote_deletion(
        self,
        credentials: AccountDeletionCredentials,
        cleanup: _DeletionCleanup | None,
    ) -> None:
        with self.state.lock:
            if not self._deletion_current_locked(credentials):
                self._settle_stale_deletion_cleanup_locked(credentials, cleanup)
                raise ApiError(self.text("network.error.sign_in_cancelled"))
            self._pending_deletion_cleanup.add(credentials.generation)
            try:
                resolved = self._resolve_deletion_cleanup_locked(cleanup)
            except (OSError, subprocess.SubprocessError):
                raise TokenCleanupPendingError(
                    "Remote account deleted. Local credential cleanup will be retried."
                ) from None
            if not resolved:
                raise TokenCleanupPendingError(
                    "Remote account deleted. Local credential cleanup will be retried."
                )

    def _settle_stale_deletion_cleanup_locked(
        self,
        credentials: AccountDeletionCredentials,
        cleanup: _DeletionCleanup | None,
    ) -> None:
        if credentials.generation != self.state.account_generation:
            self._pending_deletion_cleanup.discard(credentials.generation)
            try:
                self._remove_deletion_cleanup(cleanup)
            except (OSError, subprocess.SubprocessError):
                self._pending_deletion_cleanup.add(credentials.generation)
            return
        try:
            self._resolve_deletion_cleanup_locked(cleanup)
        except (OSError, subprocess.SubprocessError):
            self._pending_deletion_cleanup.add(credentials.generation)

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
            identity = credentials.identity
            if identity is None:
                accept_tokens(response)
            else:
                with self.token_store.account_deletion_refresh(identity):
                    accept_tokens(response)
                credentials.identity = self._current_deletion_identity_locked()
                credentials.access_token = self.state.access_token
                credentials.access_expires_at = self.state.access_expires_at
                credentials.refresh_token = self.state.refresh_token
            return self.state.access_token or ""

    def _current_deletion_identity_locked(self) -> _DeletionCredentialIdentity:
        access_token = self.state.access_token
        refresh_token = self.state.refresh_token
        if not access_token or not refresh_token:
            raise ApiError(self.text("network.error.sign_in_cancelled"))
        return _DeletionCredentialIdentity.from_tokens(
            self.api_base, access_token, refresh_token
        )

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
            self._clear_session()
            self._pending_deletion_cleanup.discard(credentials.generation)
            return True

    def fail_deletion(self, credentials: AccountDeletionCredentials) -> bool:
        with self.state.lock:
            if not self._deletion_current_locked(credentials):
                return False
            if self._deletion_cleanup_pending_locked(credentials):
                self.state.account_generation += 1
                self.state.busy = False
                self.state.deleting_account = False
                self.state.sync_queued = None
                self._clear_session()
                self._pending_deletion_cleanup.discard(credentials.generation)
                return True
            self.state.deleting_account = False
            return True

    def sign_out(self) -> LogoutCredentials:
        with self.state.lock:
            self._retry_deletion_cleanup_locked()
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
            self._retry_deletion_cleanup_locked()
            if self._has_deletion_cleanup():
                return
            if (
                self.state.shutting_down
                or generation != self.state.account_generation
                or self.state.authenticated
                or self.state.access_token
                or self.state.refresh_token
            ):
                return
            try:
                self.token_store.clear_if_signed_out()
            except (OSError, subprocess.SubprocessError):
                raise SecureStoreError(
                    "Signed out locally. Secure credential cleanup failed and will be retried."
                ) from None

    def require_authentication_ready(self) -> None:
        with self.state.lock:
            self._retry_deletion_cleanup_locked()

    def _retry_deletion_cleanup_locked(self) -> None:
        try:
            cleanup = self._resolve_pending_deletion_cleanup()
        except TokenCleanupPendingError:
            raise SecureStoreError(
                "Account deletion credential cleanup failed and will be retried."
            ) from None
        except SecureStoreError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise SecureStoreError(
                "Account deletion credential cleanup failed and will be retried."
            ) from None
        if cleanup is not None:
            self._clear_deletion_session_locked(cleanup)

    def _resolve_deletion_cleanup_locked(
        self,
        cleanup: _DeletionCleanup | None,
    ) -> bool:
        if cleanup is None:
            return True
        path = self._deletion_cleanup_path(required=False)
        if path is None:
            return True
        with (
            _account_deletion_cleanup_lock(path),
            self.token_store.account_deletion_credentials_locked(),
        ):
            identity = self.token_store.account_deletion_credential_identity_locked()
            if identity is None:
                return self._settle_absent_deletion_credentials_locked(path, cleanup)
            if not cleanup.identity.matches(identity):
                return False
            if not self.token_store.confirm_account_deletion_locked(
                cleanup.api_base, cleanup.generation, cleanup.identity
            ):
                return False
            self._rearm_deletion_cleanup_locked(path, cleanup)
            if not self._clear_deletion_credentials_locked(cleanup):
                return False
            self._clear_deletion_cleanup_marker_locked(path)
            return True

    def _settle_absent_deletion_credentials_locked(
        self, path: Path, cleanup: _DeletionCleanup
    ) -> bool:
        current = self._read_deletion_cleanup_locked(path)
        if current is None:
            return True
        if current != cleanup:
            return False
        self._clear_deletion_cleanup_marker_locked(path)
        return True

    def _rearm_deletion_cleanup_locked(
        self, path: Path, cleanup: _DeletionCleanup
    ) -> None:
        try:
            current = self._read_deletion_cleanup_locked(path)
        except SecureStoreError:
            current = None
        if current != cleanup:
            self._replace_deletion_cleanup_locked(path, cleanup.document())

    def _resolve_pending_deletion_cleanup(self) -> _DeletionCleanup | None:
        path = self._deletion_cleanup_path(required=False)
        if path is None:
            return None
        with (
            _account_deletion_cleanup_lock(path),
            self.token_store.account_deletion_credentials_locked(),
        ):
            cleanup = self._pending_or_confirmed_cleanup_locked(path)
            if cleanup is None:
                return None
            if isinstance(cleanup, _LegacyDeletionCleanup):
                self._retire_legacy_deletion_cleanup_locked(path)
                return None
            if not self._clear_deletion_credentials_locked(cleanup):
                raise TokenCleanupPendingError(
                    "Account deletion credential cleanup is pending."
                )
            self._clear_deletion_cleanup_marker_locked(path)
            return cleanup

    def _pending_or_confirmed_cleanup_locked(
        self, path: Path
    ) -> _DeletionCleanup | _LegacyDeletionCleanup | None:
        marker_error: SecureStoreError | None = None
        try:
            cleanup = self._read_deletion_cleanup_locked(path)
        except SecureStoreError as error:
            cleanup = None
            marker_error = error
        generation = self.token_store.account_deletion_confirmed_generation_locked()
        if generation is None:
            if marker_error is not None:
                raise marker_error
            return cleanup
        identity = self.token_store.account_deletion_credential_identity_locked()
        if identity is None:
            raise TokenCleanupPendingError(
                "Confirmed account deletion identity is unavailable."
            )
        confirmed = _DeletionCleanup(identity.api_base, generation, identity)
        if cleanup != confirmed:
            self._replace_deletion_cleanup_locked(path, confirmed.document())
        return confirmed

    def _retire_legacy_deletion_cleanup_locked(self, path: Path) -> None:
        identity = self.token_store.account_deletion_credential_identity_locked()
        if identity is not None:
            raise TokenCleanupPendingError(
                "Legacy account deletion cleanup requires account recovery."
            )
        self._clear_deletion_cleanup_marker_locked(path)

    def _clear_deletion_credentials_locked(
        self,
        cleanup: _DeletionCleanup,
    ) -> bool:
        return self.token_store.clear_account_deletion_credentials_locked(
            cleanup.api_base, cleanup.identity
        )

    def _clear_deletion_session_locked(self, cleanup: _DeletionCleanup) -> None:
        access_token = self.state.access_token
        refresh_token = self.state.refresh_token
        if access_token and not hmac.compare_digest(
            self._token_hash(access_token), cleanup.access_token_hash
        ):
            return
        if refresh_token and not hmac.compare_digest(
            self._token_hash(refresh_token), cleanup.refresh_token_hash
        ):
            return
        if self.state.authenticated or self.state.access_token or refresh_token:
            self.state.account_generation += 1
            self.state.busy = False
            self.state.deleting_account = False
            self.state.sync_queued = None
            self._clear_session()

    def _deletion_cleanup_pending_locked(
        self, credentials: AccountDeletionCredentials
    ) -> bool:
        return bool(
            credentials.generation in self._pending_deletion_cleanup
            or self._deletion_cleanup_exists_safely()
        )

    def _has_deletion_cleanup(self) -> bool:
        path = self._deletion_cleanup_path(required=False)
        if path is None:
            return False
        with _account_deletion_cleanup_lock(path):
            return self._read_deletion_cleanup_locked(path) is not None

    def _deletion_cleanup_exists_safely(self) -> bool:
        try:
            return self._has_deletion_cleanup()
        except OSError:
            return True

    def _read_deletion_cleanup(
        self,
    ) -> _DeletionCleanup | _LegacyDeletionCleanup | None:
        path = self._deletion_cleanup_path(required=False)
        if path is None:
            return None
        with _account_deletion_cleanup_lock(path):
            return self._read_deletion_cleanup_locked(path)

    def _read_deletion_cleanup_locked(
        self, path: Path
    ) -> _DeletionCleanup | _LegacyDeletionCleanup | None:
        if not self._path_exists(path):
            return None
        try:
            document = json.loads(_read_capped_text(path, _DELETION_CLEANUP_LIMIT))
        except FileNotFoundError:
            return None
        except _LocalDocumentTooLarge:
            raise SecureStoreError(
                "Account deletion cleanup obligation is unreadable or malformed."
            ) from None
        except (OSError, ValueError, UnicodeError):
            raise SecureStoreError(
                "Account deletion cleanup obligation is unreadable or malformed."
            ) from None
        if document == _CLEARED_DELETION_CLEANUP:
            return None
        return self._parse_deletion_cleanup(document)

    @staticmethod
    def _parse_deletion_cleanup(
        document: Any,
    ) -> _DeletionCleanup | _LegacyDeletionCleanup:
        if not isinstance(document, dict):
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        if document.get("version") == 1:
            return AccountLifecycle._parse_legacy_deletion_cleanup(document)
        if document.get("version") != 2:
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        if document.get("cleanupState") != "pending":
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        api_base = credential_api_base(document.get("apiBase"))
        generation = document.get("generation")
        valid_generation = isinstance(generation, int) and not isinstance(
            generation, bool
        )
        if not valid_generation:
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        identity = _DeletionCredentialIdentity(
            credential_api_base(document.get("credentialApiBase")),
            AccountLifecycle._parse_deletion_hash(document, "accessTokenHash"),
            AccountLifecycle._parse_deletion_hash(document, "refreshTokenHash"),
        )
        if identity.api_base != api_base:
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        return _DeletionCleanup(api_base, generation, identity)

    @staticmethod
    def _parse_legacy_deletion_cleanup(document: dict[str, Any]) -> _LegacyDeletionCleanup:
        state = document.get("credentialState")
        expected = {"version", "cleanupState", "apiBase", "generation", "credentialState"}
        if state == "refresh":
            expected.add("refreshTokenHash")
        if document.get("cleanupState") != "pending" or set(document) != expected:
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        generation = document.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        if state == "refresh":
            token_hash = AccountLifecycle._parse_deletion_hash(
                document, "refreshTokenHash"
            )
        elif state == "absent":
            token_hash = None
        else:
            raise SecureStoreError("Account deletion cleanup obligation is malformed.")
        return _LegacyDeletionCleanup(
            credential_api_base(document.get("apiBase")), generation, token_hash
        )

    @staticmethod
    def _parse_deletion_hash(document: dict[str, Any], name: str) -> str:
        token_hash = document.get(name)
        if (
            isinstance(token_hash, str)
            and len(token_hash) == 64
            and all(character in "0123456789abcdef" for character in token_hash)
        ):
            return token_hash
        raise SecureStoreError("Account deletion cleanup obligation is malformed.")

    def _write_deletion_cleanup(self, obligation: _DeletionCleanup) -> None:
        path = self._deletion_cleanup_path()
        with _account_deletion_cleanup_lock(path):
            self._write_deletion_cleanup_locked(path, obligation)

    def _write_deletion_cleanup_locked(
        self, path: Path, obligation: _DeletionCleanup
    ) -> None:
        existing = self._read_deletion_cleanup_locked(path)
        if existing == obligation:
            return
        if existing is not None:
            raise SecureStoreError("Another account deletion cleanup is pending.")
        self._replace_deletion_cleanup_locked(path, obligation.document())

    def _replace_deletion_cleanup_locked(
        self, path: Path, document: dict[str, Any]
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as cleanup_file:
                descriptor = -1
                json.dump(
                    document,
                    cleanup_file,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                cleanup_file.flush()
                os.fsync(cleanup_file.fileno())
            _replace_file_for_durable_commit(temporary_path, path)
            self._sync_deletion_cleanup_directory(path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def _remove_deletion_cleanup(
        self, cleanup: _DeletionCleanup | None
    ) -> None:
        if cleanup is None:
            return
        path = self._deletion_cleanup_path(required=False)
        if path is None:
            return
        with _account_deletion_cleanup_lock(path):
            if self._read_deletion_cleanup_locked(path) != cleanup:
                return
            self._clear_deletion_cleanup_marker_locked(path)

    def _clear_deletion_cleanup_marker_locked(self, path: Path) -> None:
        if _platform_name() == "nt":
            self._replace_deletion_cleanup_locked(
                path, _CLEARED_DELETION_CLEANUP
            )
            return
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._sync_deletion_cleanup_directory(path)

    @staticmethod
    def _sync_deletion_cleanup_directory(path: Path) -> None:
        _sync_replaced_file_directory(path)

    def _deletion_cleanup_path(self, *, required: bool = True) -> Path | None:
        cleanup_path = getattr(self.token_store, "account_deletion_cleanup_path", None)
        if callable(cleanup_path):
            return cleanup_path()
        if required:
            raise SecureStoreError("Account deletion cleanup cannot be persisted.")
        return None

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

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
