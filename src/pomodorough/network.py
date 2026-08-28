from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Protocol, cast

from platformdirs import user_config_path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtNetwork import QNetworkReply

from .localization import Strings
from .network_account import (
    AccountLifecycle,
    RevocationState,
)
from .network_revision import RevisionEventParser, RevisionStream
from .secure_store import PlatformSecretStore, SecretStore, SecureStoreError
from .network_session import (
    ApiError,
    AuthenticatedSession,
    SessionState,
    TimedDocument as TimedDocument,
)

_RevisionEventParser = RevisionEventParser

API_BASE = "https://pomodorough.egigoka.me"
RETIRED_IMPLICIT_GOOGLE_CLIENT_IDS = frozenset(
    {
        "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com",
    }
)
_DEFAULT_SECRET_STORE = object()


def _text(key: str, **values: Any) -> str:
    return Strings().text(key, **values)


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


def _request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    access_token: str | None = None,
    form: bool = False,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Pomodorough-Desktop/0.1"}
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
            body = response.read()
            return _decode_success_document(body)
    except urllib.error.HTTPError as error:
        body = error.read()
        document = None
        try:
            document = json.loads(body)
            message = (
                document.get("error_description") or document.get("error")
                if isinstance(document, dict)
                else None
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        raise ApiError(
            message or _text("network.error.http", status=error.code),
            error.code,
            document if isinstance(document, dict) else None,
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(
            _text(
                "network.error.unreachable",
                error=error.reason if hasattr(error, "reason") else error,
            )
        ) from error


def _config_root() -> Path:
    return user_config_path("pomodorough", appauthor=False, roaming=True)


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
            self.secret_store: SecretStore | None = PlatformSecretStore(
                root=_config_root() / "oauth-secrets-v1",
                service="me.egigoka.pomodorough.oauth",
                kind="oauth",
                label="Pomodorough OAuth",
            )
        else:
            self.secret_store = cast(SecretStore | None, secret_store)
        self.secret_key = f"oauth:{device_id}"

    def load(self) -> dict[str, Any] | None:
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

    def _load_legacy_secret_tool(self) -> dict[str, Any] | None:
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
            return None
        return document if isinstance(document, dict) else None

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
        stored = {
            "refreshToken": token_response["refreshToken"],
            "refreshTokenExpiresAt": token_response["refreshTokenExpiresAt"],
        }
        encoded = json.dumps(stored, separators=(",", ":"))
        if self.secret_store is not None:
            self.secret_store.save(self.secret_key, encoded.encode("utf-8"))
            try:
                self.fallback_path.unlink()
            except FileNotFoundError:
                pass
            return
        self._write_fallback(encoded)
        if shutil.which("secret-tool"):
            try:
                result = subprocess.run(
                    [
                        "secret-tool",
                        "store",
                        "--label=Pomodorough",
                        "service",
                        "pomodorough",
                        "device",
                        self.device_id,
                    ],
                    input=encoded,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            else:
                if result.returncode == 0:
                    try:
                        self.fallback_path.unlink()
                    except FileNotFoundError:
                        pass
                    return

    def clear(self) -> None:
        self._write_fallback('{"signedOut":true}')
        if self.secret_store is not None:
            self.secret_store.delete(self.secret_key)
            return
        if shutil.which("secret-tool"):
            try:
                subprocess.run(
                    ["secret-tool", "clear", "service", "pomodorough", "device", self.device_id],
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        # The tombstone remains authoritative until a later successful sign-in.
        # This keeps a stale keyring token from reviving a signed-out session.

    def _load_fallback(self) -> dict[str, Any] | None:
        try:
            document = json.loads(self.fallback_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return document if isinstance(document, dict) else None

    def _write_fallback(self, encoded: str) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.fallback_path.name}.",
            dir=self.fallback_path.parent,
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
            os.replace(temporary_path, self.fallback_path)
            if hasattr(os, "O_DIRECTORY"):
                directory_descriptor = os.open(
                    self.fallback_path.parent,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
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
        except Exception as error:  # Background boundary reports failures to UI.
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()


def _parse_oauth_credentials(source: Any) -> dict[str, str]:
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        config = document.get("installed") or document.get("web") or document
        return {
            "client_id": config["client_id"],
            "client_secret": config.get("client_secret", ""),
            "auth_uri": config.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth"),
            "token_uri": config.get("token_uri", "https://oauth2.googleapis.com/token"),
        }
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


class _CallbackHandler(BaseHTTPRequestHandler):
    result_queue: queue.Queue[dict[str, str]]

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result = {key: values[0] for key, values in query.items() if values}
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
            {"result_queue": callback_results},
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        deadline = time.monotonic() + self._callback_timeout
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
        self.api_base = api_base.rstrip("/")
        self._state = SessionState()
        self.token_store = token_store or TokenStore(device_id)
        self._request = request or (lambda *args, **kwargs: _request(*args, **kwargs))
        self._session = AuthenticatedSession(
            self.api_base,
            self._state,
            self.token_store,
            self._request,
            _text,
            lambda: datetime.now(timezone.utc),
            lambda: time.time(), lambda: time.monotonic_ns(),
        )
        self._accounts = AccountLifecycle(
            self.api_base,
            self._state,
            self.token_store,
            self._request,
            _text,
            lambda: datetime.now(timezone.utc),
        )
        self._workers: set[Worker] = set()
        self._worker_generations: dict[Worker, int] = {}
        self._revocation_workers: set[Worker] = set()
        self._revisions = RevisionStream(
            self,
            self.api_base,
            lambda: self.start_revision_stream(), lambda upper: secrets.randbelow(upper),
            lambda reply: self._valid_revision_stream_response(reply),
        )
        self._oauth_browser = oauth_browser or SystemOAuthBrowserTransport()
        self._token_urlsafe = token_urlsafe

    # Compatibility methods remain patchable, but resolve the current collaborator
    # on every call instead of retaining construction-time bound method aliases.
    def _accept_tokens(self, response: dict[str, Any]) -> None:
        self._session.accept_tokens(response)

    def _accept_login_tokens(
        self,
        response: dict[str, Any],
        expected_generation: int | None = None,
    ) -> None:
        self._session.accept_login_tokens(response, expected_generation)

    def _ensure_access(self, generation: int | None = None) -> str:
        return self._session.ensure_access(generation)

    def _authorized_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._session.authorized_request(method, path, payload)

    def _timed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        access_token: str,
    ) -> TimedDocument:
        return self._session.timed_request(
            method, path, payload, access_token=access_token
        )

    def _begin_account_deletion(self, confirmation: str) -> Any:
        return self._accounts.begin_deletion(confirmation)

    def _delete_captured_account(self, credentials: Any) -> dict[str, Any]:
        return self._accounts.delete_account(credentials)

    def _refresh_deletion_access(self, credentials: Any) -> str:
        return self._accounts.refresh_deletion_access(credentials)

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
                "platform": "windows" if sys.platform == "win32" else "linux",
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
        self.stop_revision_stream()

    def logout(self) -> None:
        credentials = self._accounts.sign_out()
        self.stop_revision_stream()
        self.signed_out.emit()
        self.status_changed.emit(self.strings.text("cloud.status.sign_in"))
        if credentials.access_token or credentials.refresh_token:
            self._start_revocation(
                credentials.access_token,
                refresh_token=credentials.refresh_token,
                access_token_is_fresh=credentials.access_token_is_fresh,
            )

    def delete_account(self, confirmation: str) -> None:
        credentials = self._begin_account_deletion(confirmation)
        if credentials is None:
            return
        self.stop_revision_stream()
        self.status_changed.emit(self.strings.text("cloud.status.syncing"))
        self._start(
            lambda: self._delete_captured_account(credentials),
            self._account_deleted,
            self._account_deletion_failed,
        )

    def _account_deleted(self, _response: dict[str, Any]) -> None:
        # Invalidate every callback tied to deleted account before notifying UI.
        self._accounts.complete_deletion()
        self.account_deleted.emit()
        self.status_changed.emit(self.strings.text("cloud.status.sign_in"))

    def _account_deletion_failed(self, error: Exception) -> None:
        self._accounts.fail_deletion()
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
    ) -> None:
        # Revocation owns a detached copy of the signed-out account's credentials.
        # It must never use or persist credentials from the current account generation.
        revocation = state or self._accounts.revocation(
            access_token,
            refresh_token,
            access_token_is_fresh,
        )
        self._launch_revocation(revocation, attempt)

    def _launch_revocation(
        self, revocation: RevocationState, attempt: int
    ) -> None:
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
        if self._state.shutting_down or attempt >= 3:
            return
        delay_ms = min(30_000, 1_000 * (2 ** (attempt - 1)))
        QTimer.singleShot(
            delay_ms,
            lambda: self._start_revocation(
                None,
                attempt=attempt,
                state=state,
            ),
        )
