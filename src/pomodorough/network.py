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
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

API_BASE = "https://pomodorough.egigoka.me"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    access_token: str | None = None,
    form: bool = False,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "Pomodorough-Linux/0.1"}
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
        try:
            document = json.loads(body)
            message = document.get("error_description") or document.get("error")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        raise ApiError(message or f"Server returned HTTP {error.code}.", error.code) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(f"Could not reach Pomodorough: {error.reason if hasattr(error, 'reason') else error}") from error


def _config_root() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pomodorough"


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
    error = Signal(str)
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
            self.signals.error.emit(str(error))
        finally:
            self.signals.finished.emit()


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
    sync_ready = Signal(object)
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

    def _start(
        self,
        function: Callable[[], Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.busy = True
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self.failure.emit)
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
            else:
                self.status_changed.emit("LOCAL • SIGN IN TO SYNC")

        def failed(message: str) -> None:
            self.access_token = None
            self.authenticated = False
            self.status_changed.emit("OFFLINE • RETRYING")
            self.failure.emit(message)

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
                    "platform": "linux",
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

        def failed(message: str) -> None:
            self.status_changed.emit("LOCAL • SIGN-IN FAILED")
            self.failure.emit(message)

        self._start(authorize, authorized, failed)

    def sync(self, payload: dict[str, Any]) -> None:
        if self.busy:
            self._sync_queued = payload
            return
        if not self.authenticated:
            return
        self.status_changed.emit("SYNCING")

        def synchronize() -> dict[str, Any]:
            token = self._ensure_access()
            try:
                return _request(
                    "POST", f"{self.api_base}/api/v1/sync", payload, access_token=token
                )
            except ApiError as error:
                if error.status != 401:
                    raise
                self.access_token = None
                token = self._ensure_access()
                return _request(
                    "POST", f"{self.api_base}/api/v1/sync", payload, access_token=token
                )

        def synchronized(response: dict[str, Any]) -> None:
            self.sync_ready.emit(response)
            self.status_changed.emit("SYNCED")

        def failed(message: str) -> None:
            self.status_changed.emit("OFFLINE • RETRYING")
            self.failure.emit(message)

        self._start(synchronize, synchronized, failed)

    def logout(self) -> None:
        if self.busy:
            return

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
