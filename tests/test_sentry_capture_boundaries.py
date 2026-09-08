"""Regression tests for silent-failure triage (D17, D24, D25, D26).

Maps each silenced boundary to its reporting contract:

- Captured (user-visible failure, report but stay non-fatal):
  iroh_network shutdown, accept-loop shed, accept-incoming refusal,
  network keyring clear.
- Silent with comment (truly expected races or best-effort mirrors):
  missing fallback unlink, consumed-tempfile unlink, best-effort parent
  chmod, OAuth signoff child spawn (D24), legacy secret-tool read/mirror
  (D24), legacy pending-id skip (D24), cursor-visibility probe (D24,
  see test_tui), foreign task-title shortcut skip (D24), room-ID
  validator rejections (D24), iroh accept-next transient (D25),
  iroh handshake peer-caused (D25), iroh serve-requests disconnect (D25),
  legacy mirror write LOW (D26), parent chmod LOW (D26).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from pomodorough import oauth_production_signoff
from pomodorough.core import task_from_title
from pomodorough.iroh_network import EndpointKeyStore, IrohService
from pomodorough.iroh_protocol import room_id_for_secret, valid_room_id
from pomodorough.network import TokenStore
from pomodorough.secure_store import SecureStoreError
from pomodorough.storage import Store
from pomodorough.storage_replication_projection import ReplicatedStateProjection


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

    def test_sync_per_peer_failure_stays_silent(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._room_id = "room-identifier"
                    service._endpoint = SimpleNamespace(connect=AsyncMock())
                    service._store = SimpleNamespace(
                        capture_local_iroh_records=Mock(),
                        iroh_peers=Mock(
                            return_value=[{
                                "endpointTicket": "ticket",
                                "endpointId": "expected",
                            }]
                        ),
                    )
                    service._emit_details = Mock()
                    statuses: list[str] = []
                    service.status_changed.connect(statuses.append)
                    fake_iroh = SimpleNamespace(
                        EndpointTicket=SimpleNamespace(
                            from_string=Mock(side_effect=ValueError("bad")),
                        )
                    )
                    with (
                        patch.dict(sys.modules, {"iroh": fake_iroh}),
                        patch(
                            "pomodorough.iroh_network.capture_exception"
                        ) as capture,
                    ):
                        result = await service._sync_known_peers()
                    capture.assert_not_called()
                    self.assertFalse(result)
                    self.assertIn("WAITING FOR PEERS", statuses)
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())


class OAuthSignoffRestartSilenceTests(unittest.TestCase):
    def test_spawn_failure_reports_false_without_capture(self) -> None:
        with patch.object(
            oauth_production_signoff.subprocess,
            "run",
            side_effect=OSError("noexec"),
        ):
            self.assertFalse(
                oauth_production_signoff._restart_in_child(
                    Path("/tmp/signoff"), "device", "fp"
                )
            )

    def test_child_exit_failure_reports_false(self) -> None:
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"
        )
        with patch.object(
            oauth_production_signoff.subprocess, "run", return_value=failed
        ):
            self.assertFalse(
                oauth_production_signoff._restart_in_child(
                    Path("/tmp/signoff"), "device", "fp"
                )
            )


class LegacySecretToolSilenceTests(unittest.TestCase):
    def _store(self, directory: str) -> TokenStore:
        return TokenStore(
            "device-1", secret_store=None, fallback_path=Path(directory) / "s.json"
        )

    def test_lookup_spawn_failure_reads_as_absent(self) -> None:
        with TemporaryDirectory() as directory:
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    side_effect=OSError("no keyring"),
                ),
                patch("pomodorough.network.capture_exception") as capture,
            ):
                self.assertIsNone(
                    self._store(directory)._load_legacy_secret_tool()
                )
            capture.assert_not_called()

    def test_malformed_legacy_blob_is_absent_unless_strict(self) -> None:
        malformed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json{{{", stderr=""
        )
        with TemporaryDirectory() as directory:
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run", return_value=malformed
                ),
                patch("pomodorough.network.capture_exception") as capture,
            ):
                store = self._store(directory)
                self.assertIsNone(store._load_legacy_secret_tool())
                with self.assertRaisesRegex(SecureStoreError, "malformed"):
                    store._load_legacy_secret_tool(strict=True)
            capture.assert_not_called()

    def test_mirror_write_failure_keeps_authoritative_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    side_effect=OSError("no keyring"),
                ),
                patch("pomodorough.network.capture_exception") as capture,
            ):
                store._save_legacy_token_locked('{"refreshToken":"r"}')
            capture.assert_not_called()
            self.assertEqual(
                store.fallback_path.read_text(encoding="utf-8"),
                '{"refreshToken":"r"}',
            )


class PendingUuid7SkipTests(unittest.TestCase):
    def test_legacy_ids_are_skipped_from_reservation(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            try:
                settings = store.load()["settings"]
                command = store.queue_command(
                    "start", None, "focus", settings["durationsMs"], now_ms=1_000
                )
                legacy = str(uuid.uuid4())
                store.connection.execute(
                    "INSERT INTO pending_task_operations (id, payload)"
                    " VALUES (?, ?)",
                    (legacy, "{}"),
                )
                store.connection.commit()
                identifiers = store._pending_uuid7_ids()
            finally:
                store.close()
            self.assertIn(command["id"], identifiers)
            self.assertNotIn(legacy, identifiers)


class ProjectionUnknownTitleSilenceTests(unittest.TestCase):
    def _inputs(
        self, title: str, task_id: str
    ) -> tuple[dict, dict, list, dict]:
        projection = ReplicatedStateProjection.__new__(ReplicatedStateProjection)
        genesis: dict = {"tasks": [], "hlcWallMs": 1, "hlcCounter": 0}
        records = [{
            "domain": "task",
            "deviceId": "device-a",
            "operation": {
                "id": "op-1",
                "type": "upsert",
                "title": title,
                "taskId": task_id,
                "hlcWallMs": 2,
                "hlcCounter": 0,
            },
        }]
        with patch.object(
            ReplicatedStateProjection,
            "_validated_room_records",
            return_value=(records, genesis),
        ):
            return projection._projection_inputs("room-1")

    def test_blank_foreign_title_skips_only_the_shortcut(self) -> None:
        _genesis, pending, _clocks, known_tasks = self._inputs("", "task-1")
        self.assertEqual(len(pending["taskOperations"]), 1)
        self.assertEqual(known_tasks, {})

    def test_valid_title_still_populates_known_tasks(self) -> None:
        task = task_from_title("Hello")
        _genesis, pending, _clocks, known_tasks = self._inputs(
            "Hello", task["id"]
        )
        self.assertEqual(len(pending["taskOperations"]), 1)
        self.assertEqual(known_tasks, {task["id"]: task})


class RoomIdValidatorSilenceTests(unittest.TestCase):
    def test_malformed_ids_are_plain_false(self) -> None:
        valid = room_id_for_secret(bytes(range(32)))
        self.assertTrue(valid_room_id(valid))
        for invalid in ("", "not a room id!!", "a", valid + "!", None, 42):
            with self.subTest(invalid=invalid):
                self.assertFalse(valid_room_id(invalid))


class IrohAcceptNextTransientSilenceTests(unittest.TestCase):
    def test_accept_next_failure_stays_silent_and_continues(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 3
                    endpoint = MagicMock()
                    endpoint.is_closed.return_value = False
                    endpoint.accept_next = AsyncMock(
                        side_effect=[RuntimeError("transient"), None]
                    )
                    service._endpoint = endpoint
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        with patch(
                            "pomodorough.iroh_network.asyncio.sleep",
                            new=AsyncMock(),
                        ):
                            await service._accept_loop(3)
                    capture.assert_not_called()
                    self.assertEqual(endpoint.accept_next.await_count, 2)
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())


class IrohHandshakeSilenceTests(unittest.TestCase):
    def test_handshake_failure_with_clean_refusal_stays_silent(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 5
                    incoming = MagicMock()
                    incoming.accept = AsyncMock(
                        side_effect=RuntimeError("bad hello")
                    )
                    incoming.ignore = AsyncMock()
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._accept_incoming(incoming, 5)
                    capture.assert_not_called()
                    incoming.ignore.assert_awaited_once()
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())

    def test_handshake_failure_with_connection_closes_silently(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 5
                    incoming = MagicMock()
                    connection = MagicMock()
                    connection.alpn.return_value = b"wrong"
                    accepted = MagicMock()
                    accepted.connect = AsyncMock(return_value=connection)
                    incoming.accept = AsyncMock(return_value=accepted)
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._accept_incoming(incoming, 5)
                    capture.assert_not_called()
                    connection.close.assert_called_once()
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())


class IrohServeRequestsSilenceTests(unittest.TestCase):
    def test_peer_disconnect_stays_silent_and_closes(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 9
                    connection = MagicMock()
                    connection.close_reason.return_value = None
                    connection.accept_bi = AsyncMock(
                        side_effect=RuntimeError("peer gone")
                    )
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._serve_requests(connection, 9)
                    capture.assert_not_called()
                    connection.close.assert_called_once_with(0, b"connection ended")
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())

    def test_idle_timeout_returns_without_capture_or_close(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                service = _iroh_service(directory)
                try:
                    service._generation = 9
                    connection = MagicMock()
                    connection.close_reason.return_value = None
                    connection.accept_bi = AsyncMock(side_effect=TimeoutError())
                    with patch(
                        "pomodorough.iroh_network.capture_exception"
                    ) as capture:
                        await service._serve_requests(connection, 9)
                    capture.assert_not_called()
                    connection.close.assert_not_called()
                finally:
                    service._loop = None
                    service._thread = None

        asyncio.run(scenario())


class LowTriageSilenceTests(unittest.TestCase):
    def test_d26_mirror_write_failure_stays_silent(self) -> None:
        with TemporaryDirectory() as directory:
            store = TokenStore(
                "device-1", secret_store=None,
                fallback_path=Path(directory) / "s.json",
            )
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    side_effect=OSError("no keyring"),
                ),
                patch("pomodorough.network.capture_exception") as capture,
            ):
                store._save_legacy_token_locked('{"refreshToken":"r"}')
            capture.assert_not_called()
            self.assertEqual(
                store.fallback_path.read_text(encoding="utf-8"),
                '{"refreshToken":"r"}',
            )

    def test_d26_parent_chmod_failure_stays_silent(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
