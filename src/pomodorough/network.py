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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Protocol

from platformdirs import user_config_path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .localization import Strings

API_BASE = "https://pomodorough.egigoka.me"


def _text(key: str, **values: Any) -> str:
    return Strings().text(key, **values)


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.document = document

    def details(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": str(self),
            "document": self.document,
        }


class TimedDocument(dict[str, Any]):
    def __init__(self, document: dict[str, Any], timing: dict[str, int]) -> None:
        super().__init__(document)
        self.timing = timing


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
            return json.loads(body) if body else {}
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
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.fallback_path = _config_root() / "session.json"

    def load(self) -> dict[str, Any] | None:
        fallback = self._load_fallback()
        if fallback is not None:
            return None if fallback.get("signedOut") is True else fallback
        if shutil.which("secret-tool"):
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", "service", "pomodorough", "device", self.device_id],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            else:
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        document = json.loads(result.stdout)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(document, dict):
                            return document
        return None

    def save(self, token_response: dict[str, Any]) -> None:
        stored = {
            "refreshToken": token_response["refreshToken"],
            "refreshTokenExpiresAt": token_response["refreshTokenExpiresAt"],
        }
        encoded = json.dumps(stored, separators=(",", ":"))
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


class _RevisionEventParser:
    def __init__(self) -> None:
        self.buffer = b""
        self.data_lines: list[bytes] = []

    def feed(self, chunk: bytes) -> list[int]:
        self.buffer += chunk
        revisions: list[int] = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                revision = self._dispatch()
                if revision is not None:
                    revisions.append(revision)
            elif line.startswith(b"data:"):
                data = line[5:]
                if data.startswith(b" "):
                    data = data[1:]
                self.data_lines.append(data)
        return revisions

    def _dispatch(self) -> int | None:
        if not self.data_lines:
            return None
        raw = b"\n".join(self.data_lines)
        self.data_lines = []
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                document = raw.decode()
            except UnicodeDecodeError:
                return None
        value = document.get("revision") if isinstance(document, dict) else document
        if isinstance(value, bool):
            return None
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return None
        return revision if revision >= 0 else None


def _read_oauth_credentials() -> dict[str, str]:
    override = os.environ.get("POMODOROUGH_GOOGLE_OAUTH_JSON")
    user_path = _config_root() / "google-oauth.json"
    source = (
        Path(override)
        if override
        else user_path
        if user_path.is_file()
        else files("pomodorough").joinpath("resources/oauth-client.json")
    )
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
    def __init__(self) -> None:
        self._cancelled = threading.Event()

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
        deadline = time.monotonic() + 180
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        try:
            opened = webbrowser.open(
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

    def __init__(
        self,
        device_id: str,
        api_base: str = API_BASE,
        oauth_browser: OAuthBrowserTransport | None = None,
        token_urlsafe: Callable[[int], str] = secrets.token_urlsafe,
        strings: Strings | None = None,
    ) -> None:
        super().__init__()
        self.strings = strings or Strings()
        self.device_id = device_id
        self.api_base = api_base.rstrip("/")
        self.token_store = TokenStore(device_id)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.authenticated = False
        self.busy = False
        self.deleting_account = False
        self._sync_queued: dict[str, Any] | None = None
        self._workers: set[Worker] = set()
        self._worker_generations: dict[Worker, int] = {}
        self._account_generation = 0
        self._revocation_workers: set[Worker] = set()
        self._network = QNetworkAccessManager(self)
        self._revision_reply: QNetworkReply | None = None
        self._revision_parser = _RevisionEventParser()
        self._revision_reconnect = QTimer(self)
        self._revision_reconnect.setSingleShot(True)
        self._revision_reconnect.timeout.connect(self.start_revision_stream)
        self._revision_reconnect_attempt = 0
        self._shutting_down = False
        self._lifecycle_lock = threading.Lock()
        self._oauth_browser = oauth_browser or SystemOAuthBrowserTransport()
        self._token_urlsafe = token_urlsafe

    def _start(
        self,
        function: Callable[[], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.busy = True
        worker = Worker(function)
        generation = self._account_generation
        self._workers.add(worker)
        self._worker_generations[worker] = generation
        worker.signals.result.connect(
            lambda result: on_result(result)
            if generation == self._account_generation
            else None
        )
        worker.signals.error.connect(
            lambda error: (
                on_error(error)
                if on_error is not None
                else self.failure.emit(str(error))
            )
            if generation == self._account_generation
            else None
        )
        worker.signals.finished.connect(lambda: self._finished(worker))
        QThreadPool.globalInstance().start(worker)

    def _finished(self, worker: Worker) -> None:
        generation = self._worker_generations.pop(
            worker, self._account_generation
        )
        self._workers.discard(worker)
        self.busy = any(
            worker_generation == self._account_generation
            for worker_generation in self._worker_generations.values()
        )
        if generation != self._account_generation:
            return
        if self._sync_queued is not None:
            payload = self._sync_queued
            self._sync_queued = None
            self.sync(payload)

    def _accept_tokens(self, response: dict[str, Any]) -> None:
        try:
            access_token = response["accessToken"]
            access_expires_value = response["accessTokenExpiresAt"]
            refresh_token = response["refreshToken"]
            refresh_expires_value = response["refreshTokenExpiresAt"]
            if not all(
                isinstance(value, str)
                for value in (
                    access_token,
                    access_expires_value,
                    refresh_token,
                    refresh_expires_value,
                )
            ):
                raise TypeError("token fields must be strings")
            access_expires_at = datetime.fromisoformat(
                access_expires_value.replace("Z", "+00:00")
            )
            refresh_expires_at = datetime.fromisoformat(
                refresh_expires_value.replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ApiError(_text("network.error.invalid_token")) from error
        if (
            not access_token.strip()
            or not refresh_token.strip()
            or access_expires_at.tzinfo is None
            or refresh_expires_at.tzinfo is None
        ):
            raise ApiError(_text("network.error.invalid_token"))
        self.token_store.save(response)
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.access_expires_at = access_expires_at

    def _accept_login_tokens(self, response: dict[str, Any]) -> None:
        with self._lifecycle_lock:
            if self._shutting_down:
                raise ApiError(_text("network.error.sign_in_cancelled"))
            self._accept_tokens(response)

    def _ensure_access(self, generation: int | None = None) -> str:
        with self._lifecycle_lock:
            refresh_generation = (
                self._account_generation if generation is None else generation
            )
            if self._shutting_down or refresh_generation != self._account_generation:
                raise ApiError(_text("network.error.sign_in_cancelled"))
            if (
                self.access_token
                and self.access_expires_at
                > datetime.now(timezone.utc) + timedelta(seconds=30)
            ):
                return self.access_token
            stored = self.token_store.load()
        if not stored or not stored.get("refreshToken"):
            raise ApiError(_text("network.error.sign_in_required"))
        try:
            response = _request(
                "POST",
                f"{self.api_base}/api/v1/auth/refresh",
                {"refreshToken": stored["refreshToken"]},
            )
        except ApiError as error:
            with self._lifecycle_lock:
                if self._shutting_down or refresh_generation != self._account_generation:
                    raise ApiError(
                        _text("network.error.sign_in_cancelled")
                    ) from error
                if error.status == 401:
                    self.token_store.clear()
            raise
        with self._lifecycle_lock:
            if self._shutting_down or refresh_generation != self._account_generation:
                raise ApiError(_text("network.error.sign_in_cancelled"))
            self._accept_tokens(response)
            return self.access_token or ""

    def _authorized_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            generation = self._account_generation
        token = self._ensure_access(generation)
        try:
            return self._timed_request(
                method, path, payload, access_token=token
            )
        except ApiError as error:
            if error.status != 401:
                raise
            with self._lifecycle_lock:
                if self._shutting_down or generation != self._account_generation:
                    raise ApiError(
                        _text("network.error.sign_in_cancelled")
                    ) from error
                self.access_token = None
            token = self._ensure_access(generation)
            return self._timed_request(
                method, path, payload, access_token=token
            )

    def _timed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        access_token: str,
    ) -> TimedDocument:
        request_physical_ms = int(time.time() * 1000)
        request_monotonic_ms = time.monotonic_ns() // 1_000_000
        document = _request(
            method,
            f"{self.api_base}{path}",
            payload,
            access_token=access_token,
        )
        received_physical_ms = int(time.time() * 1000)
        received_monotonic_ms = time.monotonic_ns() // 1_000_000
        return TimedDocument(
            document,
            {
                "requestPhysicalMs": request_physical_ms,
                "receivedPhysicalMs": received_physical_ms,
                "requestMonotonicMs": request_monotonic_ms,
                "receivedMonotonicMs": received_monotonic_ms,
            },
        )

    def restore(self) -> None:
        self.status_changed.emit(self.strings.text("cloud.status.connecting"))

        def restore_session() -> dict[str, Any] | None:
            if not self.token_store.load():
                return None
            token = self._ensure_access()
            return _request("GET", f"{self.api_base}/api/v1/me", access_token=token)["user"]

        def restored(user: dict[str, Any] | None) -> None:
            if user:
                self.authenticated = True
                self.signed_in.emit(user)
                self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
                self.start_revision_stream()
            else:
                self.status_changed.emit(self.strings.text("cloud.status.sign_in"))

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.access_token = None
            self.authenticated = False
            self.status_changed.emit(self.strings.text("cloud.status.offline_retrying"))
            self.failure.emit(str(error))

        self._start(restore_session, restored, failed)

    def login(self) -> None:
        if self.busy:
            return
        self.status_changed.emit(self.strings.text("cloud.status.waiting_google"))

        def authorized(user: dict[str, Any]) -> None:
            self.authenticated = True
            self.signed_in.emit(user)
            self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            self.status_changed.emit(self.strings.text("cloud.status.sign_in_failed"))
            self.failure.emit(str(error))

        self._start(self._authorize_google, authorized, failed)

    def _authorize_google(self) -> dict[str, Any]:
        credentials = _read_oauth_credentials()
        challenge = _request(
            "POST", f"{self.api_base}/api/v1/auth/google/challenge", {}
        )
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
        google_tokens = _request(
            "POST", credentials["token_uri"], token_payload, form=True
        )
        if not google_tokens.get("id_token"):
            raise ApiError(_text("network.error.missing_identity"))
        response = _request(
            "POST",
            f"{self.api_base}/api/v1/auth/google/exchange",
            {
                "idToken": google_tokens["id_token"],
                "challenge": challenge["challenge"],
                "deviceId": self.device_id,
                "platform": "windows" if sys.platform == "win32" else "linux",
            },
        )
        self._accept_login_tokens(response)
        user = _request(
            "GET",
            f"{self.api_base}/api/v1/me",
            access_token=self.access_token,
        )["user"]
        return user

    def sync(self, payload: dict[str, Any]) -> None:
        if self.busy:
            self._sync_queued = payload
            return
        if not self.authenticated:
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
        if self.busy or not self.authenticated:
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
        if self.busy or not self.authenticated:
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
            self._shutting_down
            or not self.authenticated
            or not self.access_token
            or self._revision_reply is not None
        ):
            return
        self._revision_reconnect.stop()
        request = QNetworkRequest(QUrl(f"{self.api_base}/api/v1/stream"))
        request.setRawHeader(b"Accept", b"text/event-stream")
        request.setRawHeader(
            b"Authorization", f"Bearer {self.access_token}".encode()
        )
        reply = self._network.get(request)
        self._revision_reply = reply
        self._revision_parser = _RevisionEventParser()
        reply.readyRead.connect(lambda reply=reply: self._read_revision_stream(reply))
        reply.finished.connect(
            lambda reply=reply: self._revision_stream_finished(reply)
        )

    def _read_revision_stream(self, reply: QNetworkReply) -> None:
        if reply is not self._revision_reply:
            return
        if not self._valid_revision_stream_response(reply):
            reply.readAll()
            return
        revisions = self._revision_parser.feed(bytes(reply.readAll()))
        if revisions:
            self._revision_reconnect_attempt = 0
        for revision in revisions:
            self.revision_available.emit(revision)

    @staticmethod
    def _valid_revision_stream_response(reply: QNetworkReply) -> bool:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        content_type = bytes(reply.rawHeader(b"Content-Type"))
        media_type = content_type.split(b";", 1)[0].strip().lower()
        return status == 200 and media_type == b"text/event-stream"

    def _schedule_revision_reconnect(self) -> None:
        base_ms = min(20_000, 1_000 * (2 ** min(self._revision_reconnect_attempt, 5)))
        jitter_ms = secrets.randbelow(min(10_000, base_ms // 2) + 1)
        self._revision_reconnect_attempt += 1
        self._revision_reconnect.start(min(30_000, base_ms + jitter_ms))

    def _revision_stream_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._revision_reply:
            reply.deleteLater()
            return
        self._read_revision_stream(reply)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        self._revision_reply = None
        reply.deleteLater()
        if self._shutting_down or not self.authenticated:
            return
        if status == 401:
            self.access_token = None
            self.authorization_stale.emit()
            return
        self._schedule_revision_reconnect()

    def stop_revision_stream(self) -> None:
        self._revision_reconnect.stop()
        self._revision_reconnect_attempt = 0
        reply = self._revision_reply
        self._revision_reply = None
        self._revision_parser = _RevisionEventParser()
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def _expire_session(self) -> None:
        self.stop_revision_stream()
        try:
            self.token_store.clear()
        except (OSError, subprocess.SubprocessError):
            pass
        self.access_token = None
        self.refresh_token = None
        self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.authenticated = False
        self.session_expired.emit()
        self.status_changed.emit(self.strings.text("cloud.status.session_expired"))

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._shutting_down = True
            # Worker signals may already be queued when aboutToQuit fires.
            # Invalidate them before the UI and store are torn down.
            self._account_generation += 1
        self.busy = False
        self.deleting_account = False
        self._sync_queued = None
        self._oauth_browser.cancel()
        self.stop_revision_stream()

    def logout(self) -> None:
        with self._lifecycle_lock:
            token = self.access_token
            refresh_token = self.refresh_token
            token_is_fresh = bool(
                token
                and self.access_expires_at
                > datetime.now(timezone.utc) + timedelta(seconds=30)
            )
            self._account_generation += 1
            self.busy = False
            self.deleting_account = False
            self._sync_queued = None
            self.token_store.clear()
            self.access_token = None
            self.refresh_token = None
            self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
            self.authenticated = False
        self.stop_revision_stream()
        self.signed_out.emit()
        self.status_changed.emit(self.strings.text("cloud.status.sign_in"))
        if token or refresh_token:
            self._start_revocation(
                token,
                refresh_token=refresh_token,
                access_token_is_fresh=token_is_fresh,
            )

    def delete_account(self, confirmation: str) -> None:
        with self._lifecycle_lock:
            if (
                confirmation != "DELETE"
                or not self.authenticated
                or self.deleting_account
            ):
                return
            access_token = self.access_token
            access_expires_at = self.access_expires_at
            refresh_token = self.refresh_token
            self._account_generation += 1
            self.deleting_account = True
        self.stop_revision_stream()
        self.status_changed.emit(self.strings.text("cloud.status.syncing"))

        def refresh_access() -> str:
            if not refresh_token:
                raise ApiError(_text("network.error.sign_in_required"))
            response = _request(
                "POST",
                f"{self.api_base}/api/v1/auth/refresh",
                {"refreshToken": refresh_token},
            )
            refreshed = response.get("accessToken")
            if not isinstance(refreshed, str) or not refreshed.strip():
                raise ApiError(_text("network.error.invalid_token"))
            return refreshed

        def delete() -> dict[str, Any]:
            token = (
                access_token
                if access_token
                and access_expires_at
                > datetime.now(timezone.utc) + timedelta(seconds=30)
                else refresh_access()
            )
            try:
                return _request(
                    "DELETE",
                    f"{self.api_base}/api/v1/account",
                    {"confirmation": "DELETE"},
                    access_token=token,
                )
            except ApiError as error:
                if error.status != 401 or token != access_token:
                    raise
                return _request(
                    "DELETE",
                    f"{self.api_base}/api/v1/account",
                    {"confirmation": "DELETE"},
                    access_token=refresh_access(),
                )

        def deleted(_response: dict[str, Any]) -> None:
            # Invalidate every callback tied to the deleted account before
            # notifying the UI to clear account-bound local state.
            self._account_generation += 1
            self.busy = False
            self.deleting_account = False
            self._sync_queued = None
            try:
                self.token_store.clear()
            except (OSError, subprocess.SubprocessError):
                pass
            self.access_token = None
            self.refresh_token = None
            self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
            self.authenticated = False
            self.account_deleted.emit()
            self.status_changed.emit(self.strings.text("cloud.status.sign_in"))

        def failed(error: Exception) -> None:
            self.deleting_account = False
            self.status_changed.emit(self.strings.text("cloud.status.sync_ready"))
            self.account_deletion_failed.emit(str(error))
            self.start_revision_stream()

        self._start(delete, deleted, failed)

    def _start_revocation(
        self,
        access_token: str | None,
        *,
        refresh_token: str | None = None,
        access_token_is_fresh: bool = True,
        attempt: int = 0,
        state: dict[str, Any] | None = None,
    ) -> None:
        # Revocation owns a detached copy of the signed-out account's credentials.
        # It must never use or persist credentials from the current account generation.
        revocation = state or {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_is_fresh": access_token_is_fresh,
        }

        def refresh_access() -> str:
            captured_refresh = revocation.get("refresh_token")
            if not isinstance(captured_refresh, str) or not captured_refresh:
                raise ApiError(_text("network.error.sign_in_required"))
            response = _request(
                "POST",
                f"{self.api_base}/api/v1/auth/refresh",
                {"refreshToken": captured_refresh},
            )
            refreshed_access = response.get("accessToken")
            if not isinstance(refreshed_access, str) or not refreshed_access.strip():
                raise ApiError(_text("network.error.invalid_token"))
            rotated_refresh = response.get("refreshToken")
            if isinstance(rotated_refresh, str) and rotated_refresh.strip():
                revocation["refresh_token"] = rotated_refresh
            revocation["access_token"] = refreshed_access
            revocation["access_token_is_fresh"] = True
            return refreshed_access

        def revoke() -> None:
            captured_access = revocation.get("access_token")
            token = (
                captured_access
                if revocation.get("access_token_is_fresh")
                and isinstance(captured_access, str)
                and captured_access
                else refresh_access()
            )
            try:
                _request(
                    "POST",
                    f"{self.api_base}/api/v1/auth/logout",
                    {},
                    access_token=token,
                )
            except ApiError as error:
                if error.status != 401 or not revocation.get("refresh_token"):
                    raise
                revocation["access_token_is_fresh"] = False
                _request(
                    "POST",
                    f"{self.api_base}/api/v1/auth/logout",
                    {},
                    access_token=refresh_access(),
                )

        worker = Worker(revoke)
        self._revocation_workers.add(worker)
        worker.signals.error.connect(
            lambda _error: self._retry_revocation(revocation, attempt + 1)
        )
        worker.signals.finished.connect(
            lambda worker=worker: self._revocation_workers.discard(worker)
        )
        QThreadPool.globalInstance().start(worker)

    def _retry_revocation(self, state: dict[str, Any], attempt: int) -> None:
        if self._shutting_down or attempt >= 3:
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
