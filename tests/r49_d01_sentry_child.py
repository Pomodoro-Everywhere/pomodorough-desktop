"""Standalone R49-D01 probe. All SDK envelopes stay in memory."""

from __future__ import annotations

import atexit
import importlib
import json
import socket
import sys
from pathlib import Path

import sentry_sdk
from sentry_sdk.transport import Transport


class MemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        self.events.extend(item.payload.json for item in envelope.items if item.type == "event")


NETWORK_ATTEMPTS: list[str] = []


def forbid_network(*args, **kwargs):
    NETWORK_ATTEMPTS.append(str(args))
    raise AssertionError("R49-D01 must not connect to any network")


def guarded_transport() -> MemoryTransport:
    transport = MemoryTransport()
    socket.socket.connect = forbid_network
    socket.socket.connect_ex = forbid_network
    socket.create_connection = forbid_network
    socket.socket.sendto = forbid_network
    socket.getaddrinfo = forbid_network
    sentry_sdk.client.make_transport = lambda *args, **kwargs: transport
    sentry_sdk.transport.make_transport = lambda *args, **kwargs: transport
    return transport


def terminal_handlers() -> tuple[list[str], list[str]]:
    from PySide6.QtCore import qInstallMessageHandler

    python_calls: list[str] = []
    qt_calls: list[str] = []

    def terminal_exception(kind, error, trace):
        python_calls.append(kind.__name__)
        sys.__excepthook__(kind, error, trace)

    sys.excepthook = terminal_exception
    qInstallMessageHandler(lambda kind, context, text: qt_calls.append(text))
    return python_calls, qt_calls


def restore_failure() -> list[str]:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QThreadPool, QTimer
    from pomodorough.network import CloudService, TokenStore

    class DeniedSecretStore:
        def load(self, key: str) -> bytes | None:
            raise PermissionError(13, "R49-D01 synthetic credential read denied")

    application = QCoreApplication.instance() or QCoreApplication([])
    service = CloudService(
        "r49-d01", "https://example.invalid",
        token_store=TokenStore(
            "r49-d01", secret_store=DeniedSecretStore(),
            fallback_path=Path("isolated-session.json"),
        ),
    )
    failures: list[str] = []
    loop = QEventLoop()
    service.failure.connect(failures.append)
    service.failure.connect(loop.quit)
    QTimer.singleShot(10000, loop.quit)
    try:
        service.restore()
        loop.exec()
        assert QThreadPool.globalInstance().waitForDone(10000)
        application.processEvents()
        assert len(failures) == 1, failures
        assert not service.authenticated
        assert application is not None
        return failures
    finally:
        service.shutdown()


def exercise_hooks(python_calls: list[str], qt_calls: list[str]) -> None:
    from PySide6.QtCore import QtMsgType, qCritical
    from pomodorough import sentry_monitoring as monitoring

    monitoring.install_exception_handlers()
    try:
        raise RuntimeError("R49-D01 synthetic Python hook")
    except RuntimeError as error:
        sys.excepthook(type(error), error, error.__traceback__)
    qCritical("R49-D01 synthetic Qt critical hook")
    # Dispatch the fatal handler without qFatal(), which would abort the process.
    monitoring._sentry_qt_message_handler(
        QtMsgType.QtFatalMsg, None, "R49-D01 synthetic Qt fatal hook",
    )
    assert python_calls == ["RuntimeError"]
    assert len(qt_calls) == 2


def write_receipt(transport, initialized, failures, python_calls, qt_calls) -> None:
    from pomodorough import sentry_monitoring as monitoring

    sentry_sdk.flush()
    result = {
        "initialized": initialized, "events": transport.events,
        "failures": failures, "python_calls": len(python_calls), "qt_calls": qt_calls,
        "fallback_count": monitoring.capture_fallback_count(),
        "runners": [name for name in ("pytest", "unittest") if name in sys.modules],
        "package": monitoring.__file__, "network_attempts": NETWORK_ATTEMPTS,
        "client_active": sentry_sdk.get_client().is_active(),
    }
    sentry_sdk.get_client().close()
    print(json.dumps(result))


def main() -> None:
    options = json.loads(sys.argv[1])
    assert "pytest" not in sys.modules and "unittest" not in sys.modules
    if options.get("runner"):
        importlib.import_module(options["runner"])
    transport = guarded_transport()
    python_calls, qt_calls = terminal_handlers()
    if options.get("preinitialized"):
        sentry_sdk.init(dsn="https://public@example.invalid/1")
        assert sys.excepthook.__module__ == "sentry_sdk.integrations.excepthook"
    from pomodorough import sentry_monitoring as monitoring

    environment = options.get("environment")
    initialized = monitoring.init_sentry_from_environment(
        env=environment, config_path=Path("missing-sentry.json"),
        force=options.get("force", False),
    )
    monitoring.install_exception_handlers()
    first_hook = sys.excepthook
    monitoring.install_exception_handlers()
    assert sys.excepthook is first_hook
    if options.get("uncaught"):
        atexit.register(write_receipt, transport, initialized, [], python_calls, qt_calls)
        return
    failures = restore_failure()
    exercise_hooks(python_calls, qt_calls)
    if options.get("external_control"):
        sentry_sdk.capture_message("R49-D01 external SDK remains active")
    write_receipt(transport, initialized, failures, python_calls, qt_calls)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
    if json.loads(sys.argv[1]).get("uncaught"):
        raise RuntimeError("R49-D01 actual uncaught termination")
