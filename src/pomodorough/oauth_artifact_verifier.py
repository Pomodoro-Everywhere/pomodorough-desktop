from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication

from .network import (
    ApiError,
    CloudService,
    SystemOAuthBrowserTransport,
    TokenStore,
    _request,
)
from .secure_store import PlatformSecretStore, SecureStoreError


TOKENS = {
    "accessToken": "access-token",
    "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
    "refreshToken": "refresh-token",
    "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
}


class _MemorySecretStore:
    def __init__(self, *, fail_save: bool = False) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_save = fail_save

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if self.fail_save:
            raise SecureStoreError("controlled secure-store failure")
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _PrivateFileSecretStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def availability(self) -> tuple[bool, str]:
        return True, "controlled private store ready"

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest

    def load(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def save(self, key: str, value: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(key)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
        path.chmod(0o600)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class _LoopbackBrowserOpener:
    def __init__(self) -> None:
        self.completed = threading.Event()
        self.failed = False

    def __call__(self, authorization_url: str, **_options: Any) -> bool:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(authorization_url).query)
        callback_query = urllib.parse.urlencode(
            {"state": query["state"][0], "code": "controlled-code"}
        )
        callback_url = f'{query["redirect_uri"][0]}?{callback_query}'

        def send_callback() -> None:
            try:
                with urllib.request.urlopen(callback_url, timeout=5) as response:
                    response.read()
            except Exception:  # noqa: BLE001 - never print callback query credentials
                self.failed = True
            finally:
                self.completed.set()

        threading.Thread(target=send_callback, daemon=True).start()
        return True


class _ScenarioHTTPServer(HTTPServer):
    def __init__(self, scenario: str) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.scenario = scenario
        self.refreshes = 0
        self.paths: list[str] = []

    def response(
        self, path: str, *, authorized: bool
    ) -> tuple[int, dict[str, Any] | list[Any]]:
        self.paths.append(path)
        if path == "/api/v1/auth/google/challenge":
            return 200, {"nonce": "controlled-nonce", "challenge": "controlled-challenge"}
        if path == "/token":
            if self.scenario == "endpoint_failure":
                return 503, {"error": "controlled endpoint failure"}
            return 200, [] if self.scenario == "malformed_response" else {"id_token": "id-token"}
        if path == "/api/v1/auth/google/exchange":
            if self.scenario == "audience_rejected":
                return 401, {"error": "controlled audience rejection"}
            return 200, dict(TOKENS)
        if path == "/api/v1/auth/refresh":
            self.refreshes += 1
            return 200, dict(TOKENS, accessToken=f"access-token-{self.refreshes + 1}")
        if path == "/api/v1/me" and authorized:
            return 200, {"user": {"id": "controlled-user"}}
        return 404, {"error": "unexpected controlled endpoint"}


class _ScenarioHandler(BaseHTTPRequestHandler):
    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        server = self.server
        status, document = server.response(  # type: ignore[attr-defined]
            urllib.parse.urlparse(self.path).path,
            authorized=bool(self.headers.get("Authorization")),
        )
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _ScenarioTransport:
    def __init__(self, scenario: str) -> None:
        self.server = _ScenarioHTTPServer(scenario)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    @property
    def refreshes(self) -> int:
        return self.server.refreshes

    @property
    def urls(self) -> list[str]:
        return self.server.paths

    def __call__(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        access_token: str | None = None,
        form: bool = False,
    ) -> dict[str, Any]:
        original_path = urllib.parse.urlparse(url).path
        path = "/token" if url == "https://oauth.example.test/token" else original_path
        return _request(
            method,
            f"{self.base_url}{path}",
            payload,
            access_token=access_token,
            form=form,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _credentials_file(root: Path) -> Path:
    path = root / "oauth.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "controlled-client",
                    "auth_uri": "https://oauth.example.test/authorize",
                    "token_uri": "https://oauth.example.test/token",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _service(
    request: _ScenarioTransport,
    store: Any,
    fallback_root: Path,
) -> tuple[CloudService, _LoopbackBrowserOpener]:
    opener = _LoopbackBrowserOpener()
    service = CloudService(
        "artifact-verifier",
        "https://api.example.test",
        oauth_browser=SystemOAuthBrowserTransport(
            open_browser=opener, callback_timeout=5
        ),
        token_urlsafe=lambda size: "state" if size == 32 else "verifier",
        token_store=TokenStore(
            "artifact-verifier",
            secret_store=store,
            fallback_path=fallback_root / "session-tombstone.json",
        ),
        request=request,
    )
    return service, opener


def _restart_command(root: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--oauth-verifier-restart-child", str(root)]
    return [
        sys.executable,
        "-m",
        "pomodorough.oauth_artifact_verifier",
        "--restart-child",
        str(root),
    ]


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("COMPROMISED_GOOGLE_CLIENT_SECRET", None)
    environment.pop("POMODOROUGH_GOOGLE_OAUTH_JSON", None)
    return environment


def _restart_in_child(root: Path) -> bool:
    result = subprocess.run(
        _restart_command(root),
        capture_output=True,
        check=False,
        env=_child_environment(),
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def _verify_restored_process(root: Path) -> bool:
    request = _ScenarioTransport("success")
    store = _PrivateFileSecretStore(root / "secure")
    service, _opener = _service(request, store, root)
    try:
        profile = service._authorized_request("GET", "/api/v1/me")
        return (
            profile.get("user", {}).get("id") == "controlled-user"
            and request.refreshes == 1
        )
    except (ApiError, SecureStoreError, OSError, ValueError):
        return False
    finally:
        service.shutdown()
        request.close()


def _success_and_restart(root: Path) -> tuple[bool, bool, bool, bool]:
    request = _ScenarioTransport("success")
    store = _PrivateFileSecretStore(root / "secure")
    first, opener = _service(request, store, root)
    try:
        user = first._authorize_google()
        signed_in = user.get("id") == "controlled-user" and bool(
            store.load("oauth:artifact-verifier")
        )
        callback_verified = opener.completed.wait(1) and not opener.failed
        http_verified = set(request.urls) >= {
            "/api/v1/auth/google/challenge",
            "/token",
            "/api/v1/auth/google/exchange",
            "/api/v1/me",
        }
    finally:
        first.shutdown()
        request.close()
    return signed_in, _restart_in_child(root), callback_verified, http_verified


def _rejected(
    scenario: str, fallback_root: Path, *, fail_store: bool = False
) -> bool:
    request = _ScenarioTransport(scenario)
    store = _MemorySecretStore(fail_save=fail_store)
    service, _opener = _service(request, store, fallback_root)
    try:
        try:
            service._authorize_google()
        except (ApiError, SecureStoreError):
            return not store.values and not any(url.endswith("/me") for url in request.urls)
        return False
    finally:
        service.shutdown()
        request.close()


def run_self_test() -> dict[str, bool]:
    QCoreApplication.instance() or QCoreApplication([])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        previous = os.environ.get("POMODOROUGH_GOOGLE_OAUTH_JSON")
        os.environ["POMODOROUGH_GOOGLE_OAUTH_JSON"] = str(
            _credentials_file(root)
        )
        try:
            signed_in, restarted, callback_verified, http_verified = (
                _success_and_restart(root)
            )
            return {
                "audience_rejected": _rejected("audience_rejected", root),
                "callback_listener_verified": callback_verified,
                "endpoint_failure_rejected": _rejected("endpoint_failure", root),
                "http_transport_verified": http_verified,
                "malformed_response_rejected": _rejected("malformed_response", root),
                "secure_store_failure_rejected": _rejected(
                    "success", root, fail_store=True
                ),
                "sign_in_verified": signed_in,
                "restart_process_verified": restarted,
            }
        finally:
            if previous is None:
                os.environ.pop("POMODOROUGH_GOOGLE_OAUTH_JSON", None)
            else:
                os.environ["POMODOROUGH_GOOGLE_OAUTH_JSON"] = previous


def _platform_store_roundtrip(store: Any) -> bool:
    available, _reason = store.availability()
    if not available:
        return False
    key = f"oauth-artifact:{secrets.token_hex(16)}"
    value = secrets.token_bytes(32)
    try:
        store.save(key, value)
        if store.load(key) != value:
            return False
        store.delete(key)
        return store.load(key) is None
    except SecureStoreError:
        return False
    finally:
        try:
            store.delete(key)
        except SecureStoreError:
            pass


def _new_platform_store(root: Path) -> PlatformSecretStore:
    return PlatformSecretStore(
        root=root,
        service="me.egigoka.pomodorough.oauth-verification",
        kind="oauth-verification",
        label="Pomodorough OAuth verification",
    )


def _verify_platform_store_child(root: Path, key: str, digest: str) -> bool:
    store = _new_platform_store(root)
    try:
        value = store.load(key)
        if value is None or not secrets.compare_digest(
            hashlib.sha256(value).hexdigest(), digest
        ):
            return False
        store.delete(key)
        return store.load(key) is None
    except SecureStoreError:
        return False


def _platform_store_child_command(root: Path, key: str, digest: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--platform-store-verifier-child",
            str(root),
            key,
            digest,
        ]
    return [
        sys.executable,
        "-m",
        "pomodorough.oauth_artifact_verifier",
        "--platform-store-child",
        str(root),
        key,
        digest,
    ]


def _platform_store_process_roundtrip(root: Path) -> bool:
    store = _new_platform_store(root)
    available, _reason = store.availability()
    if not available:
        return False
    key = f"oauth-artifact:{secrets.token_hex(16)}"
    value = secrets.token_bytes(32)
    digest = hashlib.sha256(value).hexdigest()
    try:
        store.save(key, value)
        result = subprocess.run(
            _platform_store_child_command(root, key, digest),
            capture_output=True,
            check=False,
            env=_child_environment(),
            text=True,
            timeout=30,
        )
        return result.returncode == 0 and store.load(key) is None
    except (SecureStoreError, OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            store.delete(key)
        except SecureStoreError:
            pass


def run_platform_store_test(store: Any | None = None) -> bool:
    if store is not None:
        return _platform_store_roundtrip(store)
    with tempfile.TemporaryDirectory() as directory:
        return _platform_store_process_roundtrip(Path(directory))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--platform-store-self-test", action="store_true")
    modes.add_argument("--platform-store-child", nargs=3)
    modes.add_argument("--restart-child", type=Path)
    args = parser.parse_args(argv)
    if args.platform_store_child is not None:
        root, key, digest = args.platform_store_child
        passed = _verify_platform_store_child(Path(root), key, digest)
        print(json.dumps({"platform_secure_store_child": passed}, sort_keys=True))
        return 0 if passed else 1
    if args.restart_child is not None:
        QCoreApplication.instance() or QCoreApplication([])
        passed = _verify_restored_process(args.restart_child)
        print(json.dumps({"restart_process_verified": passed}, sort_keys=True))
        return 0 if passed else 1
    if args.platform_store_self_test:
        passed = run_platform_store_test()
        print(json.dumps({"platform_secure_store": passed}, sort_keys=True))
        return 0 if passed else 1
    report = run_self_test()
    print(json.dumps(report, sort_keys=True))
    return 0 if all(report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
