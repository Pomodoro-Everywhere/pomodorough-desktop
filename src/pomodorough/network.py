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
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from platformdirs import user_config_path
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API_BASE = "https://pomodorough.egigoka.me"


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
            message or f"Server returned HTTP {error.code}.",
            error.code,
            document if isinstance(document, dict) else None,
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(f"Could not reach Pomodorough: {error.reason if hasattr(error, 'reason') else error}") from error


def _config_root() -> Path:
    return user_config_path("pomodorough", appauthor=False, roaming=True)


class TokenStore:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.fallback_path = _config_root() / "session.json"

    def load(self) -> dict[str, Any] | None:
        if shutil.which("secret-tool"):
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "pomodorough", "device", self.device_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    pass
        try:
            return json.loads(self.fallback_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, token_response: dict[str, Any]) -> None:
        stored = {
            "refreshToken": token_response["refreshToken"],
            "refreshTokenExpiresAt": token_response["refreshTokenExpiresAt"],
        }
        encoded = json.dumps(stored, separators=(",", ":"))
        if shutil.which("secret-tool"):
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
            if result.returncode == 0:
                try:
                    self.fallback_path.unlink()
                except FileNotFoundError:
                    pass
                return
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fallback_path.write_text(encoded)
        self.fallback_path.chmod(0o600)

    def clear(self) -> None:
        if shutil.which("secret-tool"):
            subprocess.run(
                ["secret-tool", "clear", "service", "pomodorough", "device", self.device_id],
                timeout=10,
                check=False,
            )
        try:
            self.fallback_path.unlink()
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
        raise ApiError(f"Google Desktop OAuth JSON not found or invalid at {source}.") from error


class _CallbackHandler(BaseHTTPRequestHandler):
    result_queue: queue.Queue[dict[str, str]]

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result = {key: values[0] for key, values in query.items() if values}
        self.result_queue.put(result)
        ok = "code" in result
        title = "Return to Pomodorough" if ok else "Sign-in did not finish"
        message = (
            "Google authorization received. Return to Pomodorough to finish sign-in."
            if ok
            else "Google sign-in was cancelled or rejected."
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

    def __init__(self, device_id: str, api_base: str = API_BASE) -> None:
        super().__init__()
        self.device_id = device_id
        self.api_base = api_base.rstrip("/")
        self.token_store = TokenStore(device_id)
        self.access_token: str | None = None
        self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.authenticated = False
        self.busy = False
        self._sync_queued: dict[str, Any] | None = None
        self._workers: set[Worker] = set()
        self._network = QNetworkAccessManager(self)
        self._revision_reply: QNetworkReply | None = None
        self._revision_parser = _RevisionEventParser()
        self._revision_reconnect = QTimer(self)
        self._revision_reconnect.setSingleShot(True)
        self._revision_reconnect.setInterval(5_000)
        self._revision_reconnect.timeout.connect(self.start_revision_stream)
        self._shutting_down = False

    def _start(
        self,
        function: Callable[[], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.busy = True
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(
            on_error or (lambda error: self.failure.emit(str(error)))
        )
        worker.signals.finished.connect(lambda: self._finished(worker))
        QThreadPool.globalInstance().start(worker)

    def _finished(self, worker: Worker) -> None:
        self._workers.discard(worker)
        self.busy = False
        if self._sync_queued is not None:
            payload = self._sync_queued
            self._sync_queued = None
            self.sync(payload)

    def _accept_tokens(self, response: dict[str, Any]) -> None:
        self.access_token = response["accessToken"]
        self.access_expires_at = datetime.fromisoformat(
            response["accessTokenExpiresAt"].replace("Z", "+00:00")
        )
        self.token_store.save(response)

    def _ensure_access(self) -> str:
        if self.access_token and self.access_expires_at > datetime.now(timezone.utc) + timedelta(seconds=30):
            return self.access_token
        stored = self.token_store.load()
        if not stored or not stored.get("refreshToken"):
            raise ApiError("Sign in to sync across devices.")
        try:
            response = _request(
                "POST",
                f"{self.api_base}/api/v1/auth/refresh",
                {"refreshToken": stored["refreshToken"]},
            )
        except ApiError as error:
            if error.status == 401:
                self.token_store.clear()
            raise
        self._accept_tokens(response)
        return self.access_token or ""

    def _authorized_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self._ensure_access()
        try:
            return _request(
                method, f"{self.api_base}{path}", payload, access_token=token
            )
        except ApiError as error:
            if error.status != 401:
                raise
            self.access_token = None
            token = self._ensure_access()
            return _request(
                method, f"{self.api_base}{path}", payload, access_token=token
            )

    def restore(self) -> None:
        self.status_changed.emit("CONNECTING")

        def restore_session() -> dict[str, Any] | None:
            if not self.token_store.load():
                return None
            token = self._ensure_access()
            return _request("GET", f"{self.api_base}/api/v1/me", access_token=token)["user"]

        def restored(user: dict[str, Any] | None) -> None:
            if user:
                self.authenticated = True
                self.signed_in.emit(user)
                self.status_changed.emit("SYNC READY")
                self.start_revision_stream()
            else:
                self.status_changed.emit("LOCAL • SIGN IN TO SYNC")

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.access_token = None
            self.authenticated = False
            self.status_changed.emit("OFFLINE • RETRYING")
            self.failure.emit(str(error))

        self._start(restore_session, restored, failed)

    def login(self) -> None:
        if self.busy:
            return
        self.status_changed.emit("WAITING FOR GOOGLE")

        def authorize() -> dict[str, Any]:
            credentials = _read_oauth_credentials()
            challenge = _request(
                "POST", f"{self.api_base}/api/v1/auth/google/challenge", {}
            )
            callback_results: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)
            handler = type("CallbackHandler", (_CallbackHandler,), {"result_queue": callback_results})
            server = HTTPServer(("127.0.0.1", 0), handler)
            server.timeout = 180
            redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            pkce = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            query = urllib.parse.urlencode(
                {
                    "client_id": credentials["client_id"],
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "nonce": challenge["nonce"],
                    "state": state,
                    "code_challenge": pkce,
                    "code_challenge_method": "S256",
                    "prompt": "select_account",
                }
            )
            opened = webbrowser.open(f'{credentials["auth_uri"]}?{query}', new=1, autoraise=True)
            if not opened:
                server.server_close()
                raise ApiError("Could not open the system browser for Google sign-in.")
            server.handle_request()
            server.server_close()
            try:
                callback = callback_results.get_nowait()
            except queue.Empty as error:
                raise ApiError("Google sign-in timed out.") from error
            if not hmac.compare_digest(callback.get("state", ""), state):
                raise ApiError("Google sign-in returned an invalid state.")
            if "code" not in callback:
                raise ApiError(callback.get("error_description") or "Google sign-in was cancelled.")
            token_payload = {
                "client_id": credentials["client_id"],
                "code": callback["code"],
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
            if credentials["client_secret"]:
                token_payload["client_secret"] = credentials["client_secret"]
            google_tokens = _request(
                "POST", credentials["token_uri"], token_payload, form=True
            )
            if not google_tokens.get("id_token"):
                raise ApiError("Google did not return an identity token.")
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
            self._accept_tokens(response)
            return _request(
                "GET", f"{self.api_base}/api/v1/me", access_token=self.access_token
            )["user"]

        def authorized(user: dict[str, Any]) -> None:
            self.authenticated = True
            self.signed_in.emit(user)
            self.status_changed.emit("SYNC READY")
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            self.status_changed.emit("LOCAL • SIGN-IN FAILED")
            self.failure.emit(str(error))

        self._start(authorize, authorized, failed)

    def sync(self, payload: dict[str, Any]) -> None:
        if self.busy:
            self._sync_queued = payload
            return
        if not self.authenticated:
            return
        self.status_changed.emit("SYNCING")

        def synchronize() -> dict[str, Any]:
            return self._authorized_request("POST", "/api/v1/sync", payload)

        def synchronized(response: dict[str, Any]) -> None:
            self.sync_ready.emit(response)
            self.status_changed.emit("SYNCED")
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit("OFFLINE • RETRYING")
            self.failure.emit(str(error))

        self._start(synchronize, synchronized, failed)

    def preview_bootstrap(self) -> None:
        if self.busy or not self.authenticated:
            return
        self.status_changed.emit("CHECKING HISTORY")

        def preview() -> dict[str, Any]:
            return self._authorized_request("GET", "/api/v1/bootstrap")

        def ready(response: dict[str, Any]) -> None:
            self.bootstrap_ready.emit(response)
            self.status_changed.emit("HISTORY DECISION")

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit("OFFLINE • HISTORY PRESERVED")
            self.failure.emit(str(error))

        self._start(preview, ready, failed)

    def resolve_bootstrap(self, payload: dict[str, Any]) -> None:
        if self.busy or not self.authenticated:
            return
        self.status_changed.emit("RESOLVING HISTORY")

        def resolve() -> dict[str, Any]:
            return self._authorized_request(
                "POST", "/api/v1/bootstrap/resolve", payload
            )

        def resolved(response: dict[str, Any]) -> None:
            self.bootstrap_resolved.emit(response)
            self.status_changed.emit("SYNCED")
            self.start_revision_stream()

        def failed(error: Exception) -> None:
            if isinstance(error, ApiError) and error.status == 409:
                self.status_changed.emit("HISTORY CONFLICT")
                self.bootstrap_conflict.emit(error.details())
                return
            if isinstance(error, ApiError) and error.status == 401:
                self._expire_session()
                return
            self.status_changed.emit("OFFLINE • HISTORY PRESERVED")
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
        for revision in self._revision_parser.feed(bytes(reply.readAll())):
            self.revision_available.emit(revision)

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
        self._revision_reconnect.start()

    def stop_revision_stream(self) -> None:
        self._revision_reconnect.stop()
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
        self.access_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.authenticated = False
        self.session_expired.emit()
        self.status_changed.emit("SESSION EXPIRED • SIGN IN AGAIN")

    def shutdown(self) -> None:
        self._shutting_down = True
        self.stop_revision_stream()

    def logout(self) -> None:
        if self.busy:
            return
        self.stop_revision_stream()

        def revoke() -> None:
            try:
                token = self._ensure_access()
                _request("POST", f"{self.api_base}/api/v1/auth/logout", {}, access_token=token)
            except ApiError:
                pass
            finally:
                self.token_store.clear()
                self.access_token = None
                self.authenticated = False

        def revoked(_result: Any) -> None:
            self.signed_out.emit()
            self.status_changed.emit("LOCAL • SIGN IN TO SYNC")

        self._start(revoke, revoked)
