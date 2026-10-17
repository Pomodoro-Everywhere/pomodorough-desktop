"""Exercise default credential restore in an isolated process and SDK transport."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

if os.environ.get("COVERAGE_PROCESS_START"):
    import coverage

    coverage.process_startup()

import sentry_sdk
from sentry_sdk.transport import Transport
from PySide6.QtCore import QCoreApplication, QEventLoop, QThreadPool, QTimer

from pomodorough import secure_store
from pomodorough.network import CloudService, TokenStore


TOKENS = {
    "accessToken": "synthetic-access",
    "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
    "refreshToken": "synthetic-refresh",
    "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
}


class MemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        self.events.extend(item.payload.json for item in envelope.items if item.type == "event")


def forbid_network(*_args, **_kwargs):
    raise AssertionError("Credential tests must not contact any network endpoint")


def setup_reporting() -> MemoryTransport:
    from pomodorough import sentry_monitoring

    transport = MemoryTransport()
    for method in ("connect", "connect_ex", "sendto"):
        setattr(socket.socket, method, forbid_network)
    socket.create_connection = forbid_network
    socket.getaddrinfo = forbid_network
    sentry_sdk.client.make_transport = lambda *args, **kwargs: transport
    assert sentry_monitoring.init_sentry_from_environment(
        env={"SENTRY_DSN": "https://public@example.invalid/1",
             "POMODOROUGH_SENTRY_ALLOW_IN_TESTS": "1"},
        config_path=Path("missing-sentry.json"),
    )
    return transport


def seed(tokens: TokenStore, mode: str) -> bytes | None:
    if mode == "valid":
        tokens.save(TOKENS)
    elif mode in {"legacy", "signed-out"}:
        document = {"signedOut": True} if mode == "signed-out" else {
            "apiBase": "https://example.invalid", "refreshToken": TOKENS["refreshToken"],
            "refreshTokenExpiresAt": TOKENS["refreshTokenExpiresAt"],
        }
        tokens.fallback_path.write_text(json.dumps(document, separators=(",", ":")))
        tokens.fallback_path.chmod(0o600)
        return tokens.fallback_path.read_bytes()
    return None


def restore(tokens: TokenStore, application) -> tuple[CloudService, list, list, list]:
    def request(method, url, payload=None, **_kwargs):
        if url.endswith("/auth/refresh"):
            assert payload == {"refreshToken": TOKENS["refreshToken"]}
            return TOKENS
        assert method == "GET" and url.endswith("/me")
        return {"user": {"id": "synthetic-user"}}

    service = CloudService("r49-d02", "https://example.invalid", token_store=tokens, request=request)
    service._revocation_restore_timer.stop()
    service._sign_out_cleanup_timer.stop()
    # No revision transport is needed for this credential boundary probe.
    service.start_revision_stream = lambda: None
    failures, statuses, users = [], [], []
    loop = QEventLoop()
    service.failure.connect(failures.append)
    service.status_changed.connect(statuses.append)
    service.signed_in.connect(users.append)
    service.failure.connect(loop.quit)
    service.signed_in.connect(loop.quit)
    service.status_changed.connect(
        lambda status: loop.quit() if status == service.strings.text("cloud.status.sign_in") else None
    )
    QTimer.singleShot(10000, loop.quit)
    service.restore()
    loop.exec()
    assert QThreadPool.globalInstance().waitForDone(10000)
    application.processEvents()
    assert failures or users or statuses[-1] == service.strings.text("cloud.status.sign_in")
    return service, failures, statuses, users


def main() -> None:
    mode = sys.argv[1]
    if "--linux-adapter" in sys.argv:
        secure_store.sys_platform = lambda: "linux"
    application = QCoreApplication([])
    tokens = TokenStore("r49-d02", fallback_path=Path("session.json"))
    tokens.bind_api("https://example.invalid")
    retained = seed(tokens, mode)
    if mode == "valid":
        restarted = subprocess.run(
            [sys.executable, __file__, "valid-restored"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        print(restarted.stdout.strip())
        return
    ready, reason = tokens.secret_store.availability()
    transport = setup_reporting()
    service, failures, statuses, users = restore(tokens, application)
    report = {
        "mode": mode,
        "ready": ready, "reason": reason, "failures": failures, "statuses": statuses,
        "users": users, "authenticated": service.authenticated,
        "fallback_exists": tokens.fallback_path.exists(),
        "retained": retained is None or (
            tokens.fallback_path.exists() and tokens.fallback_path.read_bytes() == retained
        ),
        "events": transport.events,
    }
    service.shutdown()
    sentry_sdk.flush()
    sentry_sdk.get_client().close()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
