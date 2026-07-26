from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from pomodorough.network import (
    ApiError,
    CloudService,
    TokenStore,
    _config_root,
    _request,
    _RevisionEventParser,
)


class RevisionEventParserTests(unittest.TestCase):
    def test_parses_chunked_json_and_plain_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(parser.feed(b"event: revision\nda"), [])
        self.assertEqual(
            parser.feed(b'ta: {"revision":12}\n\n: keepalive\n\ndata: 13\n\n'),
            [12, 13],
        )

    def test_ignores_invalid_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(
            parser.feed(b"data: nope\n\ndata: -1\n\ndata: true\n\n"),
            [],
        )


class OAuthResourceTests(unittest.TestCase):
    def test_config_root_uses_roaming_platform_directory(self) -> None:
        root = Path("platform-config")
        with patch("pomodorough.network.user_config_path", return_value=root) as path:
            self.assertEqual(_config_root(), root)
        path.assert_called_once_with("pomodorough", appauthor=False, roaming=True)

    def test_bundled_desktop_client(self) -> None:
        resource = files("pomodorough").joinpath("resources/oauth-client.json")
        config = json.loads(resource.read_text(encoding="utf-8"))["installed"]
        self.assertEqual(
            config["client_id"],
            "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com",
        )
        self.assertNotIn("client_secret", config)


class AuthenticationNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ensure_access_reuses_fresh_token_without_loading_or_requesting(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "fresh-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        with (
            patch.object(cloud.token_store, "load") as load,
            patch("pomodorough.network._request") as request,
        ):
            self.assertEqual(cloud._ensure_access(), "fresh-access")

        load.assert_not_called()
        request.assert_not_called()

    def test_ensure_access_rejects_missing_refresh_token_without_requesting(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshTokenExpiresAt": "2099-01-02T03:04:05Z"},
            ),
            patch("pomodorough.network._request") as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertEqual(str(raised.exception), "Sign in to sync across devices.")
        request.assert_not_called()

    def test_ensure_access_refreshes_and_persists_rotated_tokens(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        response = {
            "accessToken": "new-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "save") as save,
            patch("pomodorough.network._request", return_value=response) as request,
        ):
            self.assertEqual(cloud._ensure_access(), "new-access")

        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/auth/refresh",
            {"refreshToken": "stored-refresh"},
        )
        save.assert_called_once_with(response)
        self.assertEqual(
            cloud.access_expires_at,
            datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(cloud.access_expires_at.tzinfo)

    def test_ensure_access_clears_store_and_reraises_refresh_401(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        error = ApiError("expired refresh", 401)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "clear") as clear,
            patch("pomodorough.network._request", side_effect=error) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertIs(raised.exception, error)
        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/auth/refresh",
            {"refreshToken": "stored-refresh"},
        )
        clear.assert_called_once_with()

    def test_ensure_access_preserves_store_for_non_401_refresh_error(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        error = ApiError("unavailable", 503)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "clear") as clear,
            patch("pomodorough.network._request", side_effect=error),
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertIs(raised.exception, error)
        clear.assert_not_called()

    def test_authorized_request_retries_once_after_401_with_refreshed_token(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "stale-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        payload = {"name": "Retained payload"}
        refresh_response = {
            "accessToken": "fresh-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ) as load,
            patch.object(cloud.token_store, "save") as save,
            patch(
                "pomodorough.network._request",
                side_effect=[
                    ApiError("expired access", 401),
                    refresh_response,
                    {"ok": True},
                ],
            ) as request,
        ):
            result = cloud._authorized_request("PUT", "/api/v1/items/7", payload)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "PUT",
                    "https://example.test/api/v1/items/7",
                    payload,
                    access_token="stale-access",
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "stored-refresh"},
                ),
                call(
                    "PUT",
                    "https://example.test/api/v1/items/7",
                    payload,
                    access_token="fresh-access",
                ),
            ],
        )
        load.assert_called_once_with()
        save.assert_called_once_with(refresh_response)

    def test_authorized_request_propagates_non_401_without_retry(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "fresh-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        error = ApiError("conflict", 409)
        payload = {"expectedRevision": 4}

        with (
            patch.object(cloud.token_store, "load") as load,
            patch("pomodorough.network._request", side_effect=error) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._authorized_request("POST", "/api/v1/sync", payload)

        self.assertIs(raised.exception, error)
        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/sync",
            payload,
            access_token="fresh-access",
        )
        load.assert_not_called()

    def test_authorized_request_consecutive_401s_retry_once_and_propagate(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "stale-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        refresh_response = {
            "accessToken": "fresh-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        first_error = ApiError("expired access", 401)
        terminal_error = ApiError("session expired", 401)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ) as load,
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud.token_store, "clear") as clear,
            patch(
                "pomodorough.network._request",
                side_effect=[first_error, refresh_response, terminal_error],
            ) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._authorized_request("GET", "/api/v1/protected")

        self.assertIs(raised.exception, terminal_error)
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "GET",
                    "https://example.test/api/v1/protected",
                    None,
                    access_token="stale-access",
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "stored-refresh"},
                ),
                call(
                    "GET",
                    "https://example.test/api/v1/protected",
                    None,
                    access_token="fresh-access",
                ),
            ],
        )
        load.assert_called_once_with()
        save.assert_called_once_with(refresh_response)
        clear.assert_not_called()


class TokenStoreTests(unittest.TestCase):
    def test_fallback_roundtrip_stores_only_refresh_fields_with_private_mode(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            with (
                patch("pomodorough.network.shutil.which", return_value=None),
                patch("pomodorough.network.subprocess.run") as run,
            ):
                store.save(response)
                loaded = store.load()

            self.assertEqual(loaded, expected)
            self.assertEqual(json.loads(store.fallback_path.read_text()), expected)
            if os.name == "posix":
                self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)
            run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "fchmod"), "requires descriptor chmod")
    def test_fallback_is_private_and_complete_before_atomic_replace(self) -> None:
        response = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        real_replace = os.replace

        for existing_mode in (None, 0o644):
            with self.subTest(existing_mode=existing_mode), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1")
                store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
                if existing_mode is not None:
                    store.fallback_path.write_text("previous")
                    store.fallback_path.chmod(existing_mode)
                replacements = []

                def checked_replace(source: str | Path, destination: str | Path) -> None:
                    source_path = Path(source)
                    replacements.append(
                        (
                            source_path.stat().st_mode & 0o777,
                            json.loads(source_path.read_text()),
                            store.fallback_path.read_text()
                            if store.fallback_path.exists()
                            else None,
                        )
                    )
                    real_replace(source, destination)

                with (
                    patch("pomodorough.network.shutil.which", return_value=None),
                    patch("pomodorough.network.os.replace", side_effect=checked_replace),
                ):
                    store.save(response)

                self.assertEqual(
                    replacements,
                    [(0o600, response, "previous" if existing_mode is not None else None)],
                )
                self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)

    def test_valid_keyring_json_loads_keyring(self) -> None:
        expected = {
            "refreshToken": "keyring-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0, stdout=json.dumps(expected)),
                ),
            ):
                self.assertEqual(store.load(), expected)

    def test_fallback_from_failed_rotation_takes_precedence_over_stale_keyring(self) -> None:
        fallback = {
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        stale_keyring = {
            "refreshToken": "stale-refresh",
            "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text(json.dumps(fallback))
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0, stdout=json.dumps(stale_keyring)),
                ) as run,
            ):
                self.assertEqual(store.load(), fallback)

            run.assert_not_called()

    def test_malformed_or_non_object_keyring_json_returns_no_session(self) -> None:
        for stdout in ("{malformed", json.dumps(["not", "an", "object"])):
            with self.subTest(stdout=stdout), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1")
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run",
                        return_value=Mock(returncode=0, stdout=stdout),
                    ) as run,
                ):
                    self.assertIsNone(store.load())

                run.assert_called_once_with(
                    [
                        "secret-tool",
                        "lookup",
                        "service",
                        "pomodorough",
                        "device",
                        "device-1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

    def test_keyring_load_process_failures_return_no_session(self) -> None:
        failures = (
            subprocess.TimeoutExpired(["secret-tool"], 10),
            OSError("secret service unavailable"),
        )

        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1")
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run", side_effect=failure
                    ),
                ):
                    self.assertIsNone(store.load())

    def test_failed_keyring_store_writes_fallback(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=1),
                ) as run,
            ):
                store.save(response)

            self.assertEqual(json.loads(store.fallback_path.read_text()), expected)
        run.assert_called_once_with(
            [
                "secret-tool",
                "store",
                "--label=Pomodorough",
                "service",
                "pomodorough",
                "device",
                "device-1",
            ],
            input=json.dumps(expected, separators=(",", ":")),
            text=True,
            timeout=15,
            check=False,
        )

    def test_keyring_store_process_failures_write_fallback(self) -> None:
        response = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        failures = (
            subprocess.TimeoutExpired(["secret-tool"], 15),
            OSError("secret service unavailable"),
        )

        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1")
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run", side_effect=failure
                    ),
                ):
                    store.save(response)

                self.assertEqual(json.loads(store.fallback_path.read_text()), response)

    def test_successful_keyring_store_replaces_then_removes_stale_fallback(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")
            replacements = []
            real_replace = os.replace

            def checked_replace(source: str | Path, destination: str | Path) -> None:
                replacements.append(json.loads(Path(source).read_text()))
                real_replace(source, destination)

            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch("pomodorough.network.os.replace", side_effect=checked_replace),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                store.save(response)

            self.assertFalse(store.fallback_path.exists())
            self.assertEqual(replacements, [expected])
        run.assert_called_once_with(
            [
                "secret-tool",
                "store",
                "--label=Pomodorough",
                "service",
                "pomodorough",
                "device",
                "device-1",
            ],
            input=json.dumps(expected, separators=(",", ":")),
            text=True,
            timeout=15,
            check=False,
        )

    def test_clear_removes_keyring_and_keeps_tombstone_idempotently(self) -> None:
        command = [
            "secret-tool",
            "clear",
            "service",
            "pomodorough",
            "device",
            "device-1",
        ]

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                store.clear()
                store.clear()

            self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})
        self.assertEqual(
            run.call_args_list,
            [
                call(command, timeout=10, check=False),
                call(command, timeout=10, check=False),
            ],
        )

    def test_clear_tombstones_failed_keyring_deletion(self) -> None:
        failures = (
            Mock(returncode=1),
            subprocess.TimeoutExpired(["secret-tool"], 10),
            OSError("secret service unavailable"),
        )
        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1")
                store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
                store.fallback_path.write_text("stale")
                run_kwargs = (
                    {"return_value": failure}
                    if isinstance(failure, Mock)
                    else {"side_effect": failure}
                )
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch("pomodorough.network.subprocess.run", **run_kwargs) as run,
                ):
                    store.clear()
                    self.assertIsNone(store.load())

                self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})
                if os.name == "posix":
                    self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(run.call_count, 1)

    def test_clear_without_keyring_tombstones_session(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1")
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")

            with (
                patch("pomodorough.network.shutil.which", return_value=None),
                patch("pomodorough.network.subprocess.run") as run,
            ):
                store.clear()
                self.assertIsNone(store.load())

            run.assert_not_called()
            self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})


class BootstrapNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _run_immediately(function, on_result, on_error=None) -> None:
        try:
            on_result(function())
        except Exception as error:
            if on_error is None:
                raise
            on_error(error)

    def test_request_preserves_structured_409_response(self) -> None:
        document = {"error": "revision_conflict", "actualRevision": 4}
        http_error = urllib.error.HTTPError(
            "https://example.test/api/v1/bootstrap/resolve",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(document).encode()),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ApiError) as raised:
                _request("POST", http_error.url, {})

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.document, document)
        self.assertEqual(raised.exception.details()["status"], 409)

    def test_cloud_exposes_bootstrap_preview_and_structured_conflict(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        preview = {"revision": 4}
        preview_results = []
        conflicts = []
        cloud.bootstrap_ready.connect(preview_results.append)
        cloud.bootstrap_conflict.connect(conflicts.append)

        with (
            patch.object(cloud, "_start", side_effect=self._run_immediately),
            patch.object(cloud, "_authorized_request", return_value=preview) as call,
        ):
            cloud.preview_bootstrap()
        call.assert_called_once_with("GET", "/api/v1/bootstrap")
        self.assertEqual(preview_results, [preview])

        conflict = ApiError(
            "revision conflict", 409, {"error": "revision_conflict"}
        )
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        with (
            patch.object(cloud, "_start", side_effect=self._run_immediately),
            patch.object(cloud, "_authorized_request", side_effect=conflict) as call,
        ):
            cloud.resolve_bootstrap(payload)
        call.assert_called_once_with(
            "POST", "/api/v1/bootstrap/resolve", payload
        )
        self.assertEqual(conflicts[0]["status"], 409)
        self.assertEqual(conflicts[0]["document"]["error"], "revision_conflict")
        cloud.shutdown()

    def test_worker_preserves_409_type_for_bootstrap_conflict(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        conflict = ApiError(
            "revision conflict", 409, {"error": "revision_conflict"}
        )
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        conflicts = []
        failures = []
        loop = QEventLoop()

        def received(details: dict[str, object]) -> None:
            conflicts.append(details)
            loop.quit()

        cloud.bootstrap_conflict.connect(received)
        cloud.failure.connect(failures.append)
        with patch.object(cloud, "_authorized_request", side_effect=conflict):
            cloud.resolve_bootstrap(payload)
            QTimer.singleShot(2_000, loop.quit)
            loop.exec()

        QApplication.processEvents()
        self.assertEqual(failures, [])
        self.assertEqual(conflicts[0]["status"], 409)
        self.assertEqual(conflicts[0]["document"], {"error": "revision_conflict"})
        cloud.shutdown()

    def test_terminal_bootstrap_401_expires_session_through_worker(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        cloud.access_token = "expired-token"
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        signed_out = []
        session_expired = []
        failures = []
        statuses = []
        loop = QEventLoop()
        cloud.signed_out.connect(lambda: signed_out.append(True))
        cloud.session_expired.connect(
            lambda: (session_expired.append(True), loop.quit())
        )
        cloud.failure.connect(failures.append)
        cloud.status_changed.connect(statuses.append)

        with (
            patch.object(
                cloud,
                "_authorized_request",
                side_effect=ApiError("session expired", 401),
            ),
            patch.object(cloud.token_store, "clear") as clear,
        ):
            cloud.resolve_bootstrap(payload)
            QTimer.singleShot(2_000, loop.quit)
            loop.exec()

        QApplication.processEvents()
        clear.assert_called_once_with()
        self.assertEqual(signed_out, [])
        self.assertEqual(session_expired, [True])
        self.assertEqual(failures, [])
        self.assertFalse(cloud.authenticated)
        self.assertIsNone(cloud.access_token)
        self.assertIn("SESSION EXPIRED • SIGN IN AGAIN", statuses)
        cloud.shutdown()


if __name__ == "__main__":
    unittest.main()
