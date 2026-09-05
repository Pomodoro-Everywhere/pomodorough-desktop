from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import io
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol, cast

from platformdirs import user_config_path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtNetwork import QNetworkReply

from . import __version__
from .localization import Strings
from .network_account import (
    AccountLifecycle,
    LogoutCredentials,
    RevocationState,
    SignOutCleanupError,
    _account_deletion_cleanup_blocks_authentication,
    _account_deletion_cleanup_lock,
    _DeletionCredentialIdentity,
    _replace_file_for_durable_commit,
    _sync_replaced_file_directory,
)
from .network_revision import RevisionEventParser, RevisionStream
from .network_session import (
    ApiError,
    AuthenticatedSession,
    SessionState,
    TimedDocument,
)
from .secure_store import (
    PlatformSecretStore,
    SecretStore,
    SecureStoreError,
    TokenCleanupPendingError,
    token_store_lock,
)
from .storage_revocation import PendingSessionRevocations, credential_api_base

_RevisionEventParser = RevisionEventParser

API_BASE = "https://pomodorough.egigoka.me"
USER_AGENT = f"Pomodorough-Desktop/{__version__}"
RETIRED_IMPLICIT_GOOGLE_CLIENT_IDS = frozenset(
    {
        "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com",
    }
)
_DEFAULT_SECRET_STORE = object()
_HTTP_RESPONSE_BODY_LIMIT = 1024 * 1024
_OAUTH_CREDENTIALS_LIMIT = 64 * 1024
_FALLBACK_DOCUMENT_LIMIT = 64 * 1024


class _ResponseBodyTooLarge(Exception):
    pass


class _LocalDocumentTooLarge(Exception):
    pass


class _ReadableResponse(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


def _text(key: str, **values: Any) -> str:
    return Strings().text(key, **values)


def _desktop_oauth_platform(platform: str) -> str:
    supported = {
        "darwin": "macos",
        "linux": "linux",
        "win32": "windows",
    }
    try:
        return supported[platform]
    except KeyError as error:
        raise ApiError(
            f"{os.strerror(errno.ENOTSUP)}: {platform!r}",
            document={
                "platform": platform,
                "supportedPlatforms": sorted(supported),
            },
        ) from error


def _decode_success_document(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ApiError(_text("network.error.invalid_response")) from error
    if not isinstance(document, dict):
        raise ApiError(_text("network.error.invalid_response"))
    return document


def _read_bounded_response_body(response: _ReadableResponse) -> bytes:
    """Read through short chunks while retaining at most limit-plus-one bytes."""
    body = bytearray()
    remaining = _HTTP_RESPONSE_BODY_LIMIT + 1
    while remaining > 0:
        chunk = response.read(remaining)
        if not chunk:
            break
        retained = chunk[:remaining]
        body.extend(retained)
        remaining -= len(retained)
    if len(body) > _HTTP_RESPONSE_BODY_LIMIT:
        raise _ResponseBodyTooLarge
    return bytes(body)


def _read_capped_text(source: Any, limit: int) -> str:
    """Read a small local JSON document with an oversize rejection."""
    if isinstance(source, Path):
        if source.stat().st_size > limit:
            raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
        text = source.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > limit:
            raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
        return text
    read_bytes = getattr(source, "read_bytes", None)
    if callable(read_bytes):
        raw = read_bytes()
        if len(raw) > limit:
            raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
        return raw.decode("utf-8")
    text = source.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > limit:
        raise _LocalDocumentTooLarge(f"Local document exceeds {limit} bytes.")
    return text


def _decode_http_error(body: bytes, status: int) -> ApiError:
    try:
        document = json.loads(body)
        message = (
            document.get("error_description") or document.get("error")
            if isinstance(document, dict)
            else None
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        document = None
        message = None
    return ApiError(
        message or _text("network.error.http", status=status),
        status,
        document if isinstance(document, dict) else None,
    )


def _request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    access_token: str | None = None,
    form: bool = False,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = _read_bounded_response_body(response)
            return _decode_success_document(body)
    except urllib.error.HTTPError as error:
        with error:
            try:
                body = _read_bounded_response_body(error)
            except _ResponseBodyTooLarge:
                body = b""
        raise _decode_http_error(body, error.code) from error
    except _ResponseBodyTooLarge as error:
        raise ApiError(_text("network.error.invalid_response")) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(
            _text(
                "network.error.unreachable",
                error=error.reason if hasattr(error, "reason") else error,
            )
        ) from error


def _config_root() -> Path:
    return user_config_path("pomodorough", appauthor=False, roaming=True)


def _oauth_secret_store() -> PlatformSecretStore:
    return PlatformSecretStore(
        root=_config_root() / "oauth-secrets-v1",
        service="me.egigoka.pomodorough.oauth",
        kind="oauth",
        label="Pomodorough OAuth",
    )


@dataclass
class _FallbackCommit:
    replaced: bool = False


class TokenStore:
    def __init__(
        self,
        device_id: str,
        secret_store: SecretStore | None | object = _DEFAULT_SECRET_STORE,
        fallback_path: Path | None = None,
    ) -> None:
        self.device_id = device_id
        self.fallback_path = fallback_path or (_config_root() / "session.json")
        if secret_store is _DEFAULT_SECRET_STORE:
            self.secret_store: SecretStore | None = _oauth_secret_store()
        else:
            self.secret_store = cast(SecretStore | None, secret_store)
        self.secret_key = f"oauth:{device_id}"
        self.api_base: str | None = None
        self._deletion_refresh_identity = threading.local()
        self.revocations = PendingSessionRevocations(
            self.secret_store or _oauth_secret_store(), device_id
        )

    def bind_api(self, api_base: str) -> None:
        api_base = credential_api_base(api_base)
        if self.api_base is not None and self.api_base != api_base:
            raise SecureStoreError("Token storage is already bound to another API origin.")
        self.api_base = api_base

    def load(self) -> dict[str, Any] | None:
        stored = self._load_stored()
        if self.api_base is not None and stored and stored.get("apiBase") != self.api_base:
            return None
        return stored

    def load_for_revocation(self) -> dict[str, Any] | None:
        stored = self._load_stored()
        if not stored or "apiBase" not in stored:
            return None
        credential_api_base(stored["apiBase"])
        return stored

    def _load_stored(self) -> dict[str, Any] | None:
        with token_store_lock(self.secret_store, self.secret_key, self.fallback_path):
            return self._read_stored()

    def _read_stored(self) -> dict[str, Any] | None:
        fallback = self._load_fallback()
        if fallback is not None:
            return None if fallback.get("signedOut") is True else fallback
        if self.secret_store is not None:
            encoded = self.secret_store.load(self.secret_key)
            if encoded is None:
                return self._migrate_legacy_secret_tool()
            document = json.loads(encoded)
            return document if isinstance(document, dict) else None
        return self._load_legacy_secret_tool()

    def _load_legacy_secret_tool(
        self, *, strict: bool = False
    ) -> dict[str, Any] | None:
        if not shutil.which("secret-tool"):
            return None
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "pomodorough", "device", self.device_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            if strict:
                raise SecureStoreError("Stored OAuth credentials are malformed.") from None
            return None
        if isinstance(document, dict):
            return document
        if strict:
            raise SecureStoreError("Stored OAuth credentials are malformed.")
        return None

    def _clear_legacy_secret_tool(self) -> None:
        if not shutil.which("secret-tool"):
            raise SecureStoreError("Legacy OAuth credential cleanup is unavailable.")
        try:
            result = subprocess.run(
                ["secret-tool", "clear", "service", "pomodorough", "device", self.device_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SecureStoreError(
                f"Legacy OAuth credential cleanup failed: {error}"
            ) from error
        if result.returncode != 0:
            raise SecureStoreError(
                result.stderr.strip() or "Legacy OAuth credential cleanup was rejected."
            )

    def _migrate_legacy_secret_tool(self) -> dict[str, Any] | None:
        document = self._load_legacy_secret_tool()
        if document is None or self.secret_store is None:
            return document
        encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.secret_store.save(self.secret_key, encoded)
        try:
            self._clear_legacy_secret_tool()
        except SecureStoreError:
            try:
                self.secret_store.delete(self.secret_key)
            except Exception as rollback_error:
                raise SecureStoreError(
                    "Legacy OAuth cleanup and secure-store rollback both failed."
                ) from rollback_error
            raise
        return document

    def save(self, token_response: dict[str, Any]) -> None:
        cleanup_path = self.account_deletion_cleanup_path()
        with _account_deletion_cleanup_lock(cleanup_path), token_store_lock(
            self.secret_store, self.secret_key, self.fallback_path
        ):
            self._assert_account_deletion_unblocked_locked()
            self._assert_account_deletion_refresh_current_locked()
            self._save_locked(token_response)

    def _assert_account_deletion_unblocked_locked(self) -> None:
        marker_blocks = _account_deletion_cleanup_blocks_authentication(
            self.account_deletion_cleanup_path()
        )
        if marker_blocks:
            raise SecureStoreError(
                "Account deletion credential cleanup must finish before authentication."
            )
        if self.account_deletion_confirmed_generation_locked() is None:
            return
        raise SecureStoreError(
            "Account deletion credential cleanup must finish before authentication."
        )

    def account_deletion_cleanup_path(self) -> Path:
        identity = hashlib.sha256(self.secret_key.encode("utf-8")).hexdigest()[:16]
        return self.fallback_path.with_name(
            f".{self.fallback_path.name}.account-deletion-{identity}"
        )

    def _account_deletion_identity_path(self) -> Path:
        identity = hashlib.sha256(self.secret_key.encode("utf-8")).hexdigest()[:16]
        return self.fallback_path.with_name(
            f".{self.fallback_path.name}.account-identity-{identity}"
        )

    def _save_locked(self, token_response: dict[str, Any]) -> None:
        stored = {
            "refreshToken": token_response["refreshToken"],
            "refreshTokenExpiresAt": token_response["refreshTokenExpiresAt"],
        }
        if self.api_base is not None:
            stored["apiBase"] = self.api_base
        encoded = json.dumps(stored, separators=(",", ":"))
        if self.secret_store is not None:
            self.secret_store.save(self.secret_key, encoded.encode("utf-8"))
            try:
                self.fallback_path.unlink()
            except FileNotFoundError:
                pass
        else:
            self._save_legacy_token_locked(encoded)
        self._save_account_deletion_identity_locked(token_response)

    def _save_legacy_token_locked(self, encoded: str) -> None:
        self._write_fallback(encoded)
        if not shutil.which("secret-tool"):
            return
        try:
            result = subprocess.run(
                [
                    "secret-tool", "store", "--label=Pomodorough",
                    "service", "pomodorough", "device", self.device_id,
                ],
                input=encoded, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if result.returncode == 0:
            self.fallback_path.unlink(missing_ok=True)

    def _save_account_deletion_identity_locked(
        self, token_response: dict[str, Any]
    ) -> None:
        if self.api_base is None:
            return
        access_token = token_response.get("accessToken")
        refresh_token = token_response.get("refreshToken")
        if not isinstance(access_token, str) or not access_token:
            self._clear_account_deletion_identity_locked()
            return
        if not isinstance(refresh_token, str) or not refresh_token:
            self._clear_account_deletion_identity_locked()
            return
        identity = _DeletionCredentialIdentity.from_tokens(
            self.api_base, access_token, refresh_token
        )
        document = {
            "version": 1,
            "apiBase": identity.api_base,
            "accessTokenHash": identity.access_token_hash,
            "refreshTokenHash": identity.refresh_token_hash,
        }
        self._write_private_file(
            self._account_deletion_identity_path(),
            json.dumps(document, separators=(",", ":"), sort_keys=True),
        )

    def clear(self) -> None:
        commit = _FallbackCommit()
        try:
            with token_store_lock(self.secret_store, self.secret_key, self.fallback_path):
                self._clear_locked(commit)
        except (OSError, subprocess.SubprocessError):
            if commit.replaced:
                raise TokenCleanupPendingError("Secure credential cleanup is pending.") from None
            raise

    def clear_account_deletion_credentials(
        self,
        api_base: str,
        identity: _DeletionCredentialIdentity,
    ) -> bool:
        commit = _FallbackCommit()
        try:
            with self.account_deletion_credentials_locked():
                return self.clear_account_deletion_credentials_locked(
                    api_base, identity, commit=commit
                )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise SecureStoreError("Stored OAuth credentials are malformed.") from None
        except (OSError, subprocess.SubprocessError):
            if commit.replaced:
                raise TokenCleanupPendingError(
                    "Secure credential cleanup is pending."
                ) from None
            raise

    def clear_account_deletion_credentials_locked(
        self,
        api_base: str,
        identity: _DeletionCredentialIdentity,
        *,
        commit: _FallbackCommit | None = None,
    ) -> bool:
        stored_identity = self._account_deletion_identity_locked()
        if stored_identity is None:
            return True
        if not identity.matches(stored_identity):
            return False
        if identity.api_base != credential_api_base(api_base):
            return False
        self._clear_locked(commit)
        return True

    @contextmanager
    def account_deletion_credentials_locked(self) -> Iterator[None]:
        with token_store_lock(
            self.secret_store, self.secret_key, self.fallback_path
        ):
            yield

    @contextmanager
    def account_deletion_credential_identity(
        self,
    ) -> Iterator[_DeletionCredentialIdentity | None]:
        with self.account_deletion_credentials_locked():
            yield self._account_deletion_identity_locked()

    def account_deletion_credential_identity_locked(
        self,
    ) -> _DeletionCredentialIdentity | None:
        return self._account_deletion_identity_locked()

    def account_deletion_confirmed_generation_locked(self) -> int | None:
        identity, generation = self._load_account_deletion_record_locked()
        if generation is None:
            return None
        stored = self._account_deletion_candidate_locked()
        if stored is None:
            return generation
        if identity is None or not self._stored_credentials_match_identity(
            stored, identity
        ):
            raise SecureStoreError("Stored OAuth credential identity is unavailable.")
        return generation

    def confirm_account_deletion_locked(
        self,
        api_base: str,
        generation: int,
        identity: _DeletionCredentialIdentity,
    ) -> bool:
        current = self._account_deletion_identity_locked()
        if current is None or not identity.matches(current):
            return False
        if identity.api_base != credential_api_base(api_base):
            return False
        document = {
            "version": 2,
            "credentialState": "remoteDeletionConfirmed",
            "apiBase": identity.api_base,
            "generation": generation,
            "accessTokenHash": identity.access_token_hash,
            "refreshTokenHash": identity.refresh_token_hash,
        }
        self._write_private_file(
            self._account_deletion_identity_path(),
            json.dumps(document, separators=(",", ":"), sort_keys=True),
        )
        return True

    @contextmanager
    def account_deletion_refresh(
        self, identity: _DeletionCredentialIdentity
    ) -> Iterator[None]:
        if getattr(self._deletion_refresh_identity, "expected", None) is not None:
            raise SecureStoreError("Account deletion refresh is already active.")
        self._deletion_refresh_identity.expected = identity
        try:
            yield
        finally:
            del self._deletion_refresh_identity.expected

    def _assert_account_deletion_refresh_current_locked(self) -> None:
        expected = getattr(self._deletion_refresh_identity, "expected", None)
        if expected is None:
            return
        current = self._account_deletion_identity_locked()
        if current is not None and expected.matches(current):
            return
        raise SecureStoreError("Authenticated account changed before deletion.")

    def _account_deletion_candidate_locked(self) -> dict[str, Any] | None:
        fallback = self._load_fallback()
        if fallback is None:
            return self._read_stored()
        if fallback.get("signedOut") is not True:
            return fallback
        if self.secret_store is None:
            return self._load_legacy_secret_tool(strict=True)
        encoded = self.secret_store.load(self.secret_key)
        if encoded is None:
            return None
        document = json.loads(encoded)
        if not isinstance(document, dict):
            raise SecureStoreError("Stored OAuth credentials are malformed.")
        return document

    def _account_deletion_identity_locked(
        self,
    ) -> _DeletionCredentialIdentity | None:
        identity, _generation = self._account_deletion_record_locked()
        return identity

    def _account_deletion_record_locked(
        self,
    ) -> tuple[_DeletionCredentialIdentity | None, int | None]:
        stored = self._account_deletion_candidate_locked()
        identity, generation = self._load_account_deletion_record_locked()
        if stored is None:
            return identity, generation
        if identity is None or not self._stored_credentials_match_identity(
            stored, identity
        ):
            raise SecureStoreError("Stored OAuth credential identity is unavailable.")
        return identity, generation

    @staticmethod
    def _stored_credentials_match_identity(
        stored: dict[str, Any], identity: _DeletionCredentialIdentity
    ) -> bool:
        refresh_token = stored.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            return False
        try:
            api_base = credential_api_base(stored.get("apiBase"))
        except SecureStoreError:
            return False
        return bool(
            api_base == identity.api_base
            and hmac.compare_digest(
                hashlib.sha256(refresh_token.encode("utf-8")).hexdigest(),
                identity.refresh_token_hash,
            )
        )

    def _load_account_deletion_identity_locked(
        self,
    ) -> _DeletionCredentialIdentity | None:
        identity, _generation = self._load_account_deletion_record_locked()
        return identity

    def _load_account_deletion_record_locked(
        self,
    ) -> tuple[_DeletionCredentialIdentity | None, int | None]:
        path = self._account_deletion_identity_path()
        try:
            with path.open(encoding="utf-8") as identity_file:
                document = json.load(identity_file)
        except FileNotFoundError:
            return None, None
        except (OSError, ValueError, UnicodeError):
            raise SecureStoreError("Stored OAuth credential identity is malformed.") from None
        if document == {"version": 1, "credentialState": "cleared"}:
            return None, None
        if isinstance(document, dict) and document.get("version") == 2:
            return self._parse_confirmed_account_deletion_identity(document)
        return self._parse_account_deletion_identity(document), None

    @staticmethod
    def _parse_confirmed_account_deletion_identity(
        document: dict[str, Any],
    ) -> tuple[_DeletionCredentialIdentity, int]:
        expected = {
            "version", "credentialState", "apiBase", "generation",
            "accessTokenHash", "refreshTokenHash",
        }
        generation = document.get("generation")
        valid_generation = isinstance(generation, int) and not isinstance(
            generation, bool
        )
        if (
            set(document) != expected
            or document.get("credentialState") != "remoteDeletionConfirmed"
            or not valid_generation
        ):
            raise SecureStoreError("Stored OAuth credential identity is malformed.")
        active = dict(document)
        active.pop("credentialState")
        active.pop("generation")
        active["version"] = 1
        return TokenStore._parse_account_deletion_identity(active), generation

    @staticmethod
    def _parse_account_deletion_identity(document: Any) -> _DeletionCredentialIdentity:
        expected = {"version", "apiBase", "accessTokenHash", "refreshTokenHash"}
        if not isinstance(document, dict) or set(document) != expected:
            raise SecureStoreError("Stored OAuth credential identity is malformed.")
        if document.get("version") != 1:
            raise SecureStoreError("Stored OAuth credential identity is malformed.")
        hashes = (document.get("accessTokenHash"), document.get("refreshTokenHash"))
        if not all(TokenStore._valid_deletion_hash(value) for value in hashes):
            raise SecureStoreError("Stored OAuth credential identity is malformed.")
        return _DeletionCredentialIdentity(
            credential_api_base(document.get("apiBase")), hashes[0], hashes[1]
        )

    @staticmethod
    def _valid_deletion_hash(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def clear_if_signed_out(self) -> None:
        with token_store_lock(self.secret_store, self.secret_key, self.fallback_path):
            if self._load_cleanup_tombstone():
                self._clear_locked()

    def _load_cleanup_tombstone(self) -> bool:
        document = self._load_fallback()
        return document is not None and document.get("signedOut") is True

    def _clear_locked(self, commit: _FallbackCommit | None = None) -> None:
        self._write_fallback('{"signedOut":true}', commit=commit)
        if self.secret_store is not None:
            self.secret_store.delete(self.secret_key)
        elif shutil.which("secret-tool"):
            try:
                subprocess.run(
                    ["secret-tool", "clear", "service", "pomodorough", "device", self.device_id],
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        self._clear_account_deletion_identity_locked()
        # The tombstone remains authoritative until a later successful sign-in.
        # This keeps a stale keyring token from reviving a signed-out session.

    def _clear_account_deletion_identity_locked(self) -> None:
        path = self._account_deletion_identity_path()
        if self.api_base is None and not path.exists():
            return
        self._write_private_file(path, '{"credentialState":"cleared","version":1}')

    def _load_fallback(self) -> dict[str, Any] | None:
        try:
            document = json.loads(
                _read_capped_text(self.fallback_path, _FALLBACK_DOCUMENT_LIMIT)
            )
        except FileNotFoundError:
            return None
        except _LocalDocumentTooLarge:
            raise SecureStoreError("Local sign-out state is malformed.") from None
        except (ValueError, UnicodeError):
            raise SecureStoreError("Local sign-out state is malformed.") from None
        if not isinstance(document, dict):
            raise SecureStoreError("Local sign-out state is malformed.")
        return document

    def _write_fallback(self, encoded: str, *, commit: _FallbackCommit | None = None) -> None:
        self._write_private_file(self.fallback_path, encoded, commit=commit)

    @staticmethod
    def _write_private_file(
        path: Path, encoded: str, *, commit: _FallbackCommit | None = None
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as fallback:
                descriptor = -1
                fallback.write(encoded)
                fallback.flush()
                os.fsync(fallback.fileno())
            _replace_file_for_durable_commit(
                temporary_path, path, os
            )
            if commit is not None:
                commit.replaced = True
            _sync_replaced_file_directory(path, os)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.result.emit(self.function())
        except Exception as error:  # noqa: BLE001 - Reports boundary failures to UI.
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()


def _parse_oauth_credentials(source: Any) -> dict[str, str]:
    try:
        document = json.loads(_read_capped_text(source, _OAUTH_CREDENTIALS_LIMIT))
        config = document.get("installed") or document.get("web") or document
        return {
            "client_id": config["client_id"],
            "client_secret": config.get("client_secret", ""),
            "auth_uri": config.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth"),
            "token_uri": config.get("token_uri", "https://oauth2.googleapis.com/token"),
        }
    except _LocalDocumentTooLarge as error:
        raise ApiError(_text("network.error.oauth_config", path=source)) from error
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ApiError(_text("network.error.oauth_config", path=source)) from error


def _read_oauth_credentials() -> dict[str, str]:
    override = os.environ.get("POMODOROUGH_GOOGLE_OAUTH_JSON")
    if override:
        return _parse_oauth_credentials(Path(override))

    bundled = files("pomodorough").joinpath("resources/oauth-client.json")
    user_path = _config_root() / "google-oauth.json"
    if not user_path.is_file():
        return _parse_oauth_credentials(bundled)

    implicit = _parse_oauth_credentials(user_path)
    if (
        implicit["client_id"] in RETIRED_IMPLICIT_GOOGLE_CLIENT_IDS
        or implicit["client_secret"]
    ):
        return _parse_oauth_credentials(bundled)
    return implicit


class _CallbackReader(io.RawIOBase):
    def __init__(
        self, connection: socket.socket, deadline: float, cancelled: threading.Event,
    ) -> None:
        super().__init__()
        self.connection = connection
        self.deadline = deadline
        self.cancelled = cancelled

    def readable(self) -> bool:
        return True

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("OAuth callback read interrupted")
        return remaining

    def readinto(self, buffer: Any) -> int:
        while True:
            self.connection.settimeout(min(0.05, self.remaining()))
            try:
                return self.connection.recv_into(buffer)
            except TimeoutError:
                continue


class _CallbackHandler(BaseHTTPRequestHandler):
    result_queue: queue.Queue[dict[str, str]]
    deadline: float
    cancelled: threading.Event

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.callback_reader = _CallbackReader(
            self.connection, self.deadline, self.cancelled,
        )
        self.rfile = io.BufferedReader(self.callback_reader)

    def do_GET(self) -> None:
        self.callback_reader.remaining()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result = {key: values[0] for key, values in query.items() if values}
        self.callback_reader.remaining()
        self.result_queue.put(result)
        ok = "code" in result
        title = _text(
            "oauth.callback.success_title" if ok else "oauth.callback.failure_title"
        )
        message = (
            _text("oauth.callback.success")
            if ok
            else _text("oauth.callback.failure")
        )
        body = (
            "<!doctype html><meta charset=utf-8><title>Pomodorough</title>"
            "<style>body{margin:0;background:#dceaf1;color:#111923;font:18px sans-serif}"
            "main{max-width:36rem;margin:12vh auto;background:#f7f8f2;border:5px solid #142c5c;"
            "padding:3rem;box-shadow:12px 12px 0 #111923}h1{color:#142c5c}</style>"
            f"<main><h1>{title}</h1><p>{message}</p></main>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class OAuthBrowserTransport(Protocol):
    def authorize(
        self,
        authorization_url: Callable[[str], str],
    ) -> tuple[str, dict[str, str]]: ...

    def cancel(self) -> None: ...


class SystemOAuthBrowserTransport:
    def __init__(
        self,
        open_browser: Callable[..., bool] | None = None,
        callback_timeout: float = 180,
    ) -> None:
        self._cancelled = threading.Event()
        self._open_browser = open_browser
        self._callback_timeout = callback_timeout

    def authorize(
        self,
        authorization_url: Callable[[str], str],
    ) -> tuple[str, dict[str, str]]:
        callback_results: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)
        handler = type(
            "CallbackHandler",
            (_CallbackHandler,),
            {"result_queue": callback_results, "cancelled": self._cancelled},
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        handler.deadline = deadline = time.monotonic() + self._callback_timeout
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        try:
            opener = self._open_browser or webbrowser.open
            opened = opener(
                authorization_url(redirect_uri),
                new=1,
                autoraise=True,
            )
            if not opened:
                raise ApiError(_text("network.error.browser_open"))
            while callback_results.empty() and not self._cancelled.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                server.timeout = min(0.25, remaining)
                server.handle_request()
        finally:
            server.server_close()
        if self._cancelled.is_set():
            raise ApiError(_text("network.error.sign_in_cancelled"))
        try:
            callback = callback_results.get_nowait()
        except queue.Empty as error:
            raise ApiError(_text("network.error.sign_in_timeout")) from error
        return redirect_uri, callback

    def cancel(self) -> None:
        self._cancelled.set()


class DesktopOAuthContract:
    @staticmethod
    def authorization_url(
        credentials: dict[str, str],
        redirect_uri: str,
        nonce: str,
        state: str,
        verifier: str,
    ) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        query = urllib.parse.urlencode(
            {
                "client_id": credentials["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "nonce": nonce,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f'{credentials["auth_uri"]}?{query}'

    @staticmethod
    def authorization_code(callback: dict[str, str], expected_state: str) -> str:
        if not hmac.compare_digest(callback.get("state", ""), expected_state):
            raise ApiError(_text("network.error.invalid_state"))
        code = callback.get("code", "").strip()
        if not code:
            raise ApiError(
                callback.get("error_description")
                or callback.get("error")
                or _text("network.error.sign_in_cancelled")
            )
        return code

    @staticmethod
    def token_payload(
        credentials: dict[str, str],
        code: str,
        redirect_uri: str,
        verifier: str,
    ) -> dict[str, str]:
        payload = {
            "client_id": credentials["client_id"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if credentials.get("client_secret"):
            payload["client_secret"] = credentials["client_secret"]
        return payload


class _CollaboratorAttribute:
    """Compatibility descriptor for state now owned by explicit collaborators."""

    def __init__(self, *path: str, writable: bool = True) -> None:
        self._path = path
        self._writable = writable

    def __get__(self, instance: Any, owner: type[Any]) -> Any:
        if instance is None:
            return self
        value = instance
        for name in self._path:
            value = getattr(value, name)
        return value

    def __set__(self, instance: Any, value: Any) -> None:
        if not self._writable:
            raise AttributeError(f"{self._path[-1]} is read-only")
        target = instance
        for name in self._path[:-1]:
            target = getattr(target, name)
        setattr(target, self._path[-1], value)


class CloudService(QObject):
    status_changed = Signal(str)
    signed_in = Signal(object)
    signed_out = Signal()
    session_expired = Signal()
    sync_ready = Signal(object)
    bootstrap_ready = Signal(object)
    bootstrap_resolved = Signal(object)
    bootstrap_conflict = Signal(object)
    revision_available = Signal(object)
    authorization_stale = Signal()
    failure = Signal(str)
    account_deleted = Signal()
    account_deletion_failed = Signal(str)
    _valid_revision_stream_response = staticmethod(RevisionStream.valid_response)

    # Compatibility surface for callers and tests that historically accessed facade state.
    # CloudService itself now reads and mutates the owning collaborators directly.
    access_token = _CollaboratorAttribute("_state", "access_token")
    refresh_token = _CollaboratorAttribute("_state", "refresh_token")
    access_expires_at = _CollaboratorAttribute("_state", "access_expires_at")
    authenticated = _CollaboratorAttribute("_state", "authenticated")
    busy = _CollaboratorAttribute("_state", "busy")
    deleting_account = _CollaboratorAttribute("_state", "deleting_account")
    _sync_queued = _CollaboratorAttribute("_state", "sync_queued")
    _account_generation = _CollaboratorAttribute("_state", "account_generation")
    _shutting_down = _CollaboratorAttribute("_state", "shutting_down")
    _lifecycle_lock = _CollaboratorAttribute("_state", "lock", writable=False)
    _network = _CollaboratorAttribute("_revisions", "network")
    _revision_reply = _CollaboratorAttribute("_revisions", "state", "reply")
    _revision_parser = _CollaboratorAttribute("_revisions", "state", "parser")
    _revision_reconnect = _CollaboratorAttribute(
        "_revisions", "reconnect_timer", writable=False
    )
    _revision_reconnect_attempt = _CollaboratorAttribute(
        "_revisions", "state", "reconnect_attempt"
    )

    def __init__(
        self, device_id: str, api_base: str = API_BASE,
        oauth_browser: OAuthBrowserTransport | None = None,
        token_urlsafe: Callable[[int], str] = secrets.token_urlsafe,
        strings: Strings | None = None,
        token_store: TokenStore | None = None,
        request: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.strings = strings or Strings()
        self.device_id = device_id
        self.api_base = credential_api_base(api_base)
        self._state = SessionState()
        self.token_store = token_store or TokenStore(device_id)
        self.token_store.bind_api(self.api_base)
        self._request = request or (lambda *args, **kwargs: _request(*args, **kwargs))
        self._session = AuthenticatedSession(
            self.api_base,
            self._state,
            self.token_store,
            self._request,
            _text,
            lambda: datetime.now(UTC),
            lambda: time.time(), lambda: time.monotonic_ns(),
        )
        self._accounts = AccountLifecycle(
            self.api_base,
            self._state,
            self.token_store,
            self._request,
            _text,
            lambda: datetime.now(UTC),
            self.token_store.revocations,
        )
        self._workers: set[Worker] = set()
        self._worker_generations: dict[Worker, int] = {}
        self._revocation_workers: set[Worker] = set()
        self._configure_revocation_restore()
        self._revisions = RevisionStream(
            self,
            self.api_base,
            lambda: self.start_revision_stream(), lambda upper: secrets.randbelow(upper),
            lambda reply: self._valid_revision_stream_response(reply),
        )
        self._oauth_browser = oauth_browser or SystemOAuthBrowserTransport()
        self._token_urlsafe = token_urlsafe
        self._configure_sign_out_cleanup()

    # Compatibility methods remain patchable, but resolve the current collaborator
    # on every call instead of retaining construction-time bound method aliases.
    def _accept_tokens(self, response: dict[str, Any]) -> None:
        self._session.accept_tokens(response)

    def _accept_login_tokens(
        self,
        response: dict[str, Any],
        expected_generation: int | None = None,
    ) -> None:
        self._accounts.require_authentication_ready()
        self._session.accept_login_tokens(response, expected_generation)

    def _ensure_access(self, generation: int | None = None) -> str:
        self._accounts.require_authentication_ready()
        return self._session.ensure_access(generation)

    def _authorized_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._accounts.require_authentication_ready()
        return self._session.authorized_request(method, path, payload)

    def _timed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        access_token: str,
    ) -> TimedDocument:
        self._accounts.require_authentication_ready()
        return self._session.timed_request(
            method, path, payload, access_token=access_token
        )

    def _begin_account_deletion(self, confirmation: str) -> Any:
        return self._accounts.begin_deletion(confirmation)

    def _delete_captured_account(self, credentials: Any) -> dict[str, Any]:
        return self._accounts.delete_account(credentials, self._accept_tokens)

    def _refresh_deletion_access(self, credentials: Any) -> str:
        return self._accounts.refresh_deletion_access(credentials, self._accept_tokens)

    def _revoke_credentials(self, revocation: RevocationState) -> None:
        self._accounts.revoke(revocation)

    def _refresh_revocation_access(self, revocation: RevocationState) -> str:
        return self._accounts.refresh_revocation_access(revocation)

    def _start(
        self,
        function: Callable[[], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._state.busy = True
        worker = Worker(function)
        generation = self._state.account_generation
        self._workers.add(worker)
        self._worker_generations[worker] = generation
        worker.signals.result.connect(
            lambda result: on_result(result)
            if generation == self._state.account_generation
            else None
        )
        worker.signals.error.connect(
            lambda error: (
                on_error(error)
                if on_error is not None
                else self.failure.emit(str(error))
            )
            if generation == self._state.account_generation
            else None
        )
        worker.signals.finished.connect(lambda: self._finished(worker))
        QThreadPool.globalInstance().start(worker)

    def _finished(self, worker: Worker) -> None:
        generation = self._worker_generations.pop(
            worker, self._state.account_generation
        )
        self._workers.discard(worker)
        self._state.busy = any(
            worker_generation == self._state.account_generation
            for worker_generation in self._worker_generations.values()
        )
        if generation != self._state.account_generation:
            return
        if self._state.sync_queued is not None:
            payload = self._state.sync_queued
            self._state.sync_queued = None
            self.sync(payload)

    def restore(self) -> None:
        self.status_changed.emit(self.strings.text("cloud.status.connecting"))

        def restore_session() -> dict[str, Any] | None:
            self._accounts.require_authentication_ready()
            if not self.token_store.load():
                return None
            token = self._ensure_access()
            return self._request(
                "GET", f"{self.api_base}/api/v1/me", access_token=token
            )["user"]

        def restored(user: dict[str, Any] | None) -> None:
            if user:
                self._state.authenticated = True
                self.signed_in.emit(user)
                self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
                self.start_revision_stream()
            else:
                self.status_changed.emit(self.strings.text("cloud.status.sign_in"))

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self._state.access_token = None
            self._state.authenticated = False
            self.status_changed.emit(self.strings.text("cloud.status.offline_retrying"))
            self.failure.emit(str(error))

        self._start(restore_session, restored, failed)

    def login(self) -> None:
        if self._state.busy:
            return
        try:
            self._accounts.require_authentication_ready()
        except SecureStoreError as error:
            self.status_changed.emit(self.strings.text("cloud.status.sign_in_failed"))
            self.failure.emit(str(error))
            return
        self.status_changed.emit(self.strings.text("cloud.status.waiting_google"))

        def authorized(user: dict[str, Any]) -> None:
            self._state.authenticated = True
            self.signed_in.emit(user)
            self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            self.status_changed.emit(self.strings.text("cloud.status.sign_in_failed"))
            self.failure.emit(str(error))

        with self._state.lock:
            generation = self._state.account_generation
        self._start(
            lambda: self._authorize_google(expected_generation=generation),
            authorized,
            failed,
        )

    def _authorize_google(
        self,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        generation = self._authorization_generation(expected_generation)
        credentials = _read_oauth_credentials()
        challenge = self._request(
            "POST", f"{self.api_base}/api/v1/auth/google/challenge", {}
        )
        identity_token = self._google_identity_token(credentials, challenge)
        response = self._exchange_google_identity(identity_token, challenge)
        access_token = self._access_token_after_login(response, generation)
        user = self._request(
            "GET",
            f"{self.api_base}/api/v1/me",
            access_token=access_token,
        )["user"]
        self._assert_authorization_generation(generation)
        return user

    def _authorization_generation(
        self, expected_generation: int | None
    ) -> int:
        with self._state.lock:
            generation = (
                self._state.account_generation
                if expected_generation is None
                else expected_generation
            )
            if self._state.shutting_down or generation != self._state.account_generation:
                raise ApiError(_text("network.error.sign_in_cancelled"))
            return generation

    def _assert_authorization_generation(self, generation: int) -> None:
        with self._state.lock:
            if self._state.shutting_down or generation != self._state.account_generation:
                raise ApiError(_text("network.error.sign_in_cancelled"))

    def _google_identity_token(
        self,
        credentials: dict[str, str],
        challenge: dict[str, Any],
    ) -> str:
        state = self._token_urlsafe(32)
        verifier = self._token_urlsafe(64)
        redirect_uri, callback = self._oauth_browser.authorize(
            lambda redirect: DesktopOAuthContract.authorization_url(
                credentials,
                redirect,
                challenge["nonce"],
                state,
                verifier,
            )
        )
        code = DesktopOAuthContract.authorization_code(callback, state)
        token_payload = DesktopOAuthContract.token_payload(
            credentials,
            code,
            redirect_uri,
            verifier,
        )
        google_tokens = self._request(
            "POST", credentials["token_uri"], token_payload, form=True
        )
        identity_token = google_tokens.get("id_token")
        if not identity_token:
            raise ApiError(_text("network.error.missing_identity"))
        return identity_token

    def _exchange_google_identity(
        self,
        identity_token: str,
        challenge: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{self.api_base}/api/v1/auth/google/exchange",
            {
                "idToken": identity_token,
                "challenge": challenge["challenge"],
                "deviceId": self.device_id,
                "platform": _desktop_oauth_platform(sys.platform),
            },
        )

    def _access_token_after_login(
        self,
        response: dict[str, Any],
        generation: int,
    ) -> str | None:
        self._accept_login_tokens(response, expected_generation=generation)
        with self._state.lock:
            if self._state.shutting_down or generation != self._state.account_generation:
                raise ApiError(_text("network.error.sign_in_cancelled"))
            return self._state.access_token

    def sync(self, payload: dict[str, Any]) -> None:
        if self._state.busy:
            self._state.sync_queued = payload
            return
        if not self._state.authenticated:
            return
        self.status_changed.emit(self.strings.text("cloud.status.syncing"))

        def synchronize() -> dict[str, Any]:
            return self._authorized_request("POST", "/api/v1/sync", payload)

        def synchronized(response: dict[str, Any]) -> None:
            self.sync_ready.emit(response)
            self.status_changed.emit(self.strings.text("cloud.status.synced"))
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit(self.strings.text("cloud.status.offline_retrying"))
            self.failure.emit(str(error))

        self._start(synchronize, synchronized, failed)

    def preview_bootstrap(self) -> None:
        if self._state.busy or not self._state.authenticated:
            return
        self.status_changed.emit(self.strings.text("cloud.status.checking_history"))

        def preview() -> dict[str, Any]:
            return self._authorized_request("GET", "/api/v1/bootstrap")

        def ready(response: dict[str, Any]) -> None:
            self.bootstrap_ready.emit(response)
            self.status_changed.emit(self.strings.text("cloud.status.history_decision"))

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit(self.strings.text("cloud.status.history_preserved"))
            self.failure.emit(str(error))

        self._start(preview, ready, failed)

    def resolve_bootstrap(self, payload: dict[str, Any]) -> None:
        if self._state.busy or not self._state.authenticated:
            return
        self.status_changed.emit(self.strings.text("cloud.status.resolving_history"))

        def resolve() -> dict[str, Any]:
            return self._authorized_request(
                "POST", "/api/v1/bootstrap/resolve", payload
            )

        def resolved(response: dict[str, Any]) -> None:
            self.bootstrap_resolved.emit(response)
            self.status_changed.emit(self.strings.text("cloud.status.synced"))
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 409:
                self.status_changed.emit(self.strings.text("cloud.status.history_conflict"))
                self.bootstrap_conflict.emit(error.details())
                return
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit(self.strings.text("cloud.status.history_preserved"))
            self.failure.emit(str(error))

        self._start(resolve, resolved, failed)

    def start_revision_stream(self) -> None:
        if (
            self._state.shutting_down
            or not self._state.authenticated
            or not self._state.access_token
            or self._revisions.state.reply is not None
        ):
            return
        try:
            self._accounts.require_authentication_ready()
        except SecureStoreError as error:
            self.failure.emit(str(error))
            return
        self._revisions.start(
            self._state.access_token,
            self._read_revision_stream,
            self._revision_stream_finished,
        )

    def _read_revision_stream(self, reply: QNetworkReply) -> None:
        for revision in self._revisions.read(reply):
            self.revision_available.emit(revision)

    def _schedule_revision_reconnect(self) -> None:
        self._revisions.schedule_reconnect()

    def _revision_stream_finished(self, reply: QNetworkReply) -> None:
        finished = self._revisions.finish(reply)
        if not finished.was_active:
            return
        for revision in finished.revisions:
            self.revision_available.emit(revision)
        if self._state.shutting_down or not self._state.authenticated:
            return
        if finished.status == 401:
            self._state.access_token = None
            self.authorization_stale.emit()
            return
        self._schedule_revision_reconnect()

    def stop_revision_stream(self) -> None:
        self._revisions.stop()

    def _expire_session(self) -> None:
        self.stop_revision_stream()
        self._accounts.expire_session()
        self.session_expired.emit()
        self.status_changed.emit(self.strings.text("cloud.status.session_expired"))

    def shutdown(self) -> None:
        with self._state.lock:
            self._state.shutting_down = True
            # Worker signals may already be queued when aboutToQuit fires.
            # Invalidate them before the UI and store are torn down.
            self._state.account_generation += 1
        self._state.busy = False
        self._state.deleting_account = False
        self._state.sync_queued = None
        self._oauth_browser.cancel()
        self._revocation_restore_timer.stop()
        self._sign_out_cleanup_timer.stop()
        self.stop_revision_stream()

    def logout(self) -> None:
        try:
            credentials = self._accounts.sign_out()
        except SignOutCleanupError as error:
            QTimer.singleShot(
                1000, lambda generation=error.generation: self._retry_sign_out_cleanup(generation)
            )
            self._publish_sign_out(error.credentials)
            self.failure.emit(str(error))
            raise
        except (OSError, subprocess.SubprocessError):
            message = "Sign out could not be persisted. Session retained; retry sign out."
            self.failure.emit(message)
            raise SecureStoreError(message) from None
        self._publish_sign_out(credentials)

    def _publish_sign_out(self, credentials: LogoutCredentials) -> None:
        self.stop_revision_stream()
        self.signed_out.emit()
        self.status_changed.emit(self.strings.text("cloud.status.sign_in"))
        if credentials.access_token or credentials.refresh_token:
            self._start_revocation(
                credentials.access_token,
                refresh_token=credentials.refresh_token,
                access_token_is_fresh=credentials.access_token_is_fresh,
                identifier=credentials.identifier,
                api_base=credentials.api_base,
            )

    def _configure_sign_out_cleanup(self) -> None:
        generation = self._state.account_generation
        self._sign_out_cleanup_timer = QTimer(self)
        self._sign_out_cleanup_timer.setSingleShot(True)
        self._sign_out_cleanup_timer.timeout.connect(
            lambda: self._retry_sign_out_cleanup(generation)
        )
        self._sign_out_cleanup_timer.start(0)

    def _retry_sign_out_cleanup(self, generation: int, attempt: int = 1) -> None:
        try:
            self._accounts.retry_sign_out_cleanup(generation)
        except SecureStoreError as error:
            self.failure.emit(str(error))
            attempt = min(attempt + 1, 6)
            QTimer.singleShot(
                min(30_000, 1000 * 2 ** (attempt - 1)),
                lambda: self._retry_sign_out_cleanup(generation, attempt),
            )

    def delete_account(self, confirmation: str) -> None:
        credentials = self._begin_account_deletion(confirmation)
        if credentials is None:
            return
        self.stop_revision_stream()
        self.status_changed.emit(self.strings.text("cloud.status.syncing"))
        self._start(
            lambda: self._delete_captured_account(credentials),
            lambda response: self._account_deleted(response, credentials),
            lambda error: self._account_deletion_failed(error, credentials),
        )

    def _account_deleted(self, _response: dict[str, Any], credentials: Any) -> None:
        # Invalidate every callback tied to deleted account before notifying UI.
        if not self._accounts.complete_deletion(credentials):
            return
        self.account_deleted.emit()
        self.status_changed.emit(self.strings.text("cloud.status.sign_in"))

    def _account_deletion_failed(self, error: Exception, credentials: Any) -> None:
        if not self._accounts.fail_deletion(credentials):
            return
        self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
        self.account_deletion_failed.emit(str(error))
        self.start_revision_stream()

    def _start_revocation(
        self,
        access_token: str | None,
        *,
        refresh_token: str | None = None,
        access_token_is_fresh: bool = True,
        attempt: int = 0,
        state: RevocationState | None = None,
        identifier: str | None = None,
        api_base: str | None = None,
    ) -> None:
        # Revocation owns a detached copy of the signed-out account's credentials.
        # It must never use or persist credentials from the current account generation.
        revocation = state or self._accounts.revocation(
            access_token,
            refresh_token,
            access_token_is_fresh,
            identifier,
            api_base,
        )
        identity = (revocation.api_base, revocation.identifier)
        if self._state.shutting_down or identity in self._revocation_ids:
            return
        self._revocation_ids.add(identity)
        self._launch_revocation(revocation, attempt)

    def _launch_revocation(
        self, revocation: RevocationState, attempt: int
    ) -> None:
        if self._state.shutting_down:
            return
        worker = Worker(lambda: self._revoke_credentials(revocation))
        self._revocation_workers.add(worker)
        worker.signals.error.connect(
            lambda _error: self._retry_revocation(revocation, attempt + 1)
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._revocation_workers.discard(worker)
        )
        QThreadPool.globalInstance().start(worker)

    def _retry_revocation(self, state: RevocationState, attempt: int) -> None:
        if self._state.shutting_down:
            return
        attempt = min(attempt, 6)
        delay_ms = min(30_000, 1_000 * (2 ** (attempt - 1)))
        QTimer.singleShot(
            delay_ms,
            lambda: self._launch_revocation(state, attempt),
        )

    def _configure_revocation_restore(self) -> None:
        self._revocation_ids: set[tuple[str, str]] = set()
        self._revocation_restore_attempt = 0
        self._revocation_restore_timer = QTimer(self)
        self._revocation_restore_timer.setSingleShot(True)
        self._revocation_restore_timer.timeout.connect(self._restore_revocations)
        self._revocation_restore_timer.start(0)

    def _restore_revocations(self) -> None:
        if self._state.shutting_down:
            return
        worker = Worker(self._accounts.pending_revocations)
        self._revocation_workers.add(worker)
        worker.signals.result.connect(self._resume_revocations)
        worker.signals.error.connect(self._retry_revocation_restore)
        worker.signals.finished.connect(
            lambda: self._revocation_workers.discard(worker)
        )
        QThreadPool.globalInstance().start(worker)

    def _resume_revocations(self, pending: list[RevocationState]) -> None:
        self._revocation_restore_attempt = 0
        for revocation in pending:
            self._start_revocation(None, state=revocation)

    def _retry_revocation_restore(self, _error: Exception) -> None:
        if self._state.shutting_down:
            return
        self._revocation_restore_attempt = min(self._revocation_restore_attempt + 1, 6)
        self._revocation_restore_timer.start(
            min(30_000, 1_000 * 2 ** (self._revocation_restore_attempt - 1))
        )
