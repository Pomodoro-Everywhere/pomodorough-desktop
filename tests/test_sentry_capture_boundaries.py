"""Regression tests for silent-failure triage (D17).

Maps each silenced boundary to its reporting contract:

- Captured (user-visible failure, report but stay non-fatal):
  iroh_network shutdown, accept-loop shed, accept-incoming refusal,
  network keyring clear.
- Silent with comment (truly expected races): missing fallback unlink,
  consumed-tempfile unlink, best-effort parent chmod.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from pomodorough.iroh_network import EndpointKeyStore, IrohService
from pomodorough.network import TokenStore
from pomodorough.storage import Store


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _iroh_service(directory: str) -> IrohService:
    return IrohService(
        Path(directory) / "state.sqlite3",
        "device-12345678",
        key_store=EndpointKeyStore(_MemorySecretStore()),
    )


class IrohShutdownCaptureTests(unittest.TestCase):
    def test_shutdown_reports_teardown_failure(self) -> None:
        with TemporaryDirectory() as directory:
            service = _iroh_service(directory)
            service._loop = MagicMock()
            service._thread = MagicMock()

            def _failing_submit(coroutine, loop):
                coroutine.close()
                future = MagicMock()
                future.result.side_effect = RuntimeError("shutdown hung")
                return future

            with (
                patch(
                    "asyncio.run_coroutine_threadsafe",
                    side_effect=_failing_submit,
                ),
                patch(
                    "pomodorough.iroh_network.capture_exception"
                ) as capture,
            ):
                service.shutdown()
            capture.assert_called_once()
            self.assertIsInstance(capture.call_args[0][0], RuntimeError)


class IrohAcceptLoopCaptureTests(unittest.TestCase):
    def test_shed_handshake_reports_failed_ignore(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 7
                    endpoint = MagicMock()
                    endpoint.is_closed.return_value = False
                    incoming = MagicMock()
                    incoming.ignore = AsyncMock(
                        side_effect=RuntimeError("peer gone")
                    )
                    endpoint.accept_next = AsyncMock(side_effect=[incoming, None])
                    service._endpoint = endpoint
                    service._connection_tasks = {
                        object() for _ in range(IrohService.MAX_PENDING_HANDSHAKES)
                    }
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._accept_loop(7)
                    capture.assert_called_once()
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())

    def test_refused_handshake_reports_failed_ignore(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 5
                    incoming = MagicMock()
                    incoming.accept = AsyncMock(
                        side_effect=RuntimeError("refused")
                    )
                    incoming.ignore = AsyncMock(
                        side_effect=RuntimeError("gone")
                    )
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._accept_incoming(incoming, 5)
                    capture.assert_called_once()
                    self.assertIsInstance(capture.call_args[0][0], RuntimeError)
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())


class NetworkKeyringCaptureTests(unittest.TestCase):
    def test_clear_locked_reports_keyring_failure(self) -> None:
        with TemporaryDirectory() as directory:
            fallback = Path(directory) / "session.json"
            store = TokenStore(
                "device-1", secret_store=None, fallback_path=fallback
            )
            with (
                patch("pomodorough.network.capture_exception") as capture,
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    side_effect=OSError("no keyring"),
                ),
            ):
                store._clear_locked()
            capture.assert_called_once()
            self.assertIsInstance(capture.call_args[0][0], OSError)
            self.assertTrue(fallback.exists())


class ExpectedSilenceTests(unittest.TestCase):
    def test_save_locked_missing_fallback_stays_silent(self) -> None:
        with TemporaryDirectory() as directory:
            fallback = Path(directory) / "session.json"
            store = TokenStore(
                "device-1",
                secret_store=_MemorySecretStore(),
                fallback_path=fallback,
            )
            response = {
                "accessToken": "access-secret",
                "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
                "refreshToken": "refresh-secret",
                "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
            }
            with patch("pomodorough.network.capture_exception") as capture:
                store._save_locked(response)
            capture.assert_not_called()
            self.assertFalse(fallback.exists())

    def test_write_private_file_missing_temp_stays_silent(self) -> None:
        with TemporaryDirectory() as directory:
            target = Path(directory) / "session.json"
            with patch("pomodorough.network.capture_exception") as capture:
                TokenStore._write_private_file(target, '{"ok":true}')
            capture.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), '{"ok":true}')

    def test_open_database_chmod_failure_stays_silent(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store.__new__(Store)
            store.path = Path(directory) / "probe.sqlite3"
            with (
                patch(
                    "pomodorough.sentry_monitoring.capture_exception"
                ) as capture,
                patch.object(Path, "chmod", side_effect=OSError("readonly fs")),
            ):
                store._open_database(restrict_existing_parent=True)
            try:
                capture.assert_not_called()
                row = store.connection.execute("select 1").fetchone()
                self.assertEqual(row[0], 1)
            finally:
                store.connection.close()

    def test_shutdown_without_loop_stays_silent(self) -> None:
        with TemporaryDirectory() as directory:
            service = _iroh_service(directory)
            with patch(
                "pomodorough.sentry_monitoring.capture_exception"
            ) as capture:
                service.shutdown()
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
