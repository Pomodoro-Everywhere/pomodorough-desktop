from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pomodorough.oauth_artifact_verifier import (
    TOKENS,
    _PrivateFileSecretStore,
    _RestoredSessionRequest,
    _child_environment,
    _restart_command,
    _restart_in_child,
    _verify_restored_process,
    main,
    run_platform_store_test,
)
from pomodorough.network import ApiError, TokenStore


ROOT = Path(__file__).parents[1]


class _MemoryPlatformStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def availability(self) -> tuple[bool, str]:
        return True, "ready"

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class OAuthArtifactVerifierTests(unittest.TestCase):
    def test_platform_store_roundtrip_deletes_controlled_secret(self) -> None:
        store = _MemoryPlatformStore()

        self.assertTrue(run_platform_store_test(store))
        self.assertEqual(store.values, {})

    def test_platform_store_child_loads_persisted_value_and_deletes_it(self) -> None:
        store = _MemoryPlatformStore()
        value = b"controlled-platform-value"
        store.save("controlled-key", value)
        digest = hashlib.sha256(value).hexdigest()
        with patch(
            "pomodorough.oauth_artifact_verifier._new_platform_store",
            return_value=store,
        ), patch.object(sys, "stdout", None):
            result = main(
                [
                    "--platform-store-child",
                    "controlled-root",
                    "controlled-key",
                    digest,
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(store.values, {})

    def test_platform_store_parent_uses_child_without_exposing_value(self) -> None:
        store = _MemoryPlatformStore()
        value = b"controlled-platform-value"
        child_commands: list[list[str]] = []

        def run_child(command, **_kwargs):
            arguments = [str(part) for part in command]
            child_commands.append(arguments)
            self.assertIn("--platform-store-child", arguments)
            self.assertNotIn(value.decode("ascii"), arguments)
            self.assertNotIn(value.hex(), arguments)
            self.assertNotIn(base64.b64encode(value).decode("ascii"), arguments)
            self.assertNotIn("COMPROMISED_GOOGLE_CLIENT_SECRET", _kwargs["env"])
            self.assertNotIn("POMODOROUGH_GOOGLE_OAUTH_JSON", _kwargs["env"])
            key, digest = arguments[-2:]
            self.assertEqual(digest, hashlib.sha256(value).hexdigest())
            store.delete(key)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.dict(
            os.environ,
            {
                "COMPROMISED_GOOGLE_CLIENT_SECRET": "scan-only-secret",
                "POMODOROUGH_GOOGLE_OAUTH_JSON": "controlled-path",
            },
        ), patch(
            "pomodorough.oauth_artifact_verifier._new_platform_store",
            return_value=store,
        ), patch(
            "pomodorough.oauth_artifact_verifier.secrets.token_hex",
            return_value="controlled-key",
        ), patch(
            "pomodorough.oauth_artifact_verifier.secrets.token_bytes",
            return_value=value,
        ), patch(
            "pomodorough.oauth_artifact_verifier.subprocess.run",
            side_effect=run_child,
        ):
            self.assertTrue(run_platform_store_test())

        self.assertEqual(len(child_commands), 1)
        self.assertEqual(store.values, {})

    def test_windowed_artifact_uses_exit_status_without_stdout(self) -> None:
        with patch(
            "pomodorough.oauth_artifact_verifier.run_platform_store_test",
            return_value=True,
        ), patch.object(sys, "stdout", None):
            result = main(["--platform-store-self-test"])

        self.assertEqual(result, 0)

    def test_windowed_restart_child_uses_exit_status_without_stdout(self) -> None:
        for returncode, expected in ((0, True), (1, False)):
            completed = subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout="", stderr=""
            )
            with self.subTest(returncode=returncode), patch(
                "pomodorough.oauth_artifact_verifier.subprocess.run",
                return_value=completed,
            ):
                self.assertEqual(
                    _restart_in_child(Path("controlled-state")), expected
                )

    def test_windowed_restart_child_fails_closed_at_timeout(self) -> None:
        with patch(
            "pomodorough.oauth_artifact_verifier.subprocess.run",
            side_effect=subprocess.TimeoutExpired([], 30),
        ) as run:
            self.assertFalse(_restart_in_child(Path("controlled-state")))

        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_restart_verifier_avoids_qt_service_lifecycle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = TokenStore(
                "artifact-verifier",
                secret_store=_PrivateFileSecretStore(root / "secure"),
                fallback_path=root / "session-tombstone.json",
            )
            store.bind_api("https://api.example.test")
            store.save(TOKENS)
            with patch(
                "pomodorough.oauth_artifact_verifier.CloudService",
                side_effect=AssertionError("restart child constructed Qt service"),
            ), patch(
                "pomodorough.oauth_artifact_verifier._ScenarioTransport",
                side_effect=AssertionError("restart child started loopback server"),
            ):
                self.assertTrue(_verify_restored_process(root))

    def test_restored_session_transport_rejects_unexpected_requests(self) -> None:
        request = _RestoredSessionRequest()
        cases = (
            ("GET", "https://api.example.test/api/v1/me", None, None, False),
            (
                "POST",
                "https://api.example.test/api/v1/auth/refresh",
                {"refreshToken": "wrong-token"},
                None,
                False,
            ),
            (
                "POST",
                "https://other.example.test/api/v1/auth/refresh",
                {"refreshToken": TOKENS["refreshToken"]},
                None,
                False,
            ),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ApiError):
                request(*arguments)

    def test_restart_child_reads_persisted_session_in_separate_process(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = TokenStore(
                "artifact-verifier",
                secret_store=_PrivateFileSecretStore(root / "secure"),
                fallback_path=root / "session-tombstone.json",
            )
            store.bind_api("https://api.example.test")
            store.save(TOKENS)

            result = subprocess.run(
                _restart_command(root),
                capture_output=True,
                check=False,
                env=_child_environment(),
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout), {"restart_process_verified": True}
            )
            restored = store.load()
            self.assertIsNotNone(restored)
            self.assertEqual(restored["refreshToken"], TOKENS["refreshToken"])
            self.assertNotIn("accessToken", restored)
            combined_output = result.stdout + result.stderr
            self.assertNotIn("access-token", combined_output)
            self.assertNotIn("refresh-token", combined_output)

    def test_packaged_self_test_covers_success_restart_and_negative_contracts(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pomodorough.oauth_artifact_verifier", "--self-test"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "audience_rejected": True,
                "callback_listener_verified": True,
                "endpoint_failure_rejected": True,
                "http_transport_verified": True,
                "malformed_response_rejected": True,
                "secure_store_failure_rejected": True,
                "sign_in_verified": True,
                "restart_process_verified": True,
            },
        )
        combined_output = result.stdout + result.stderr
        for controlled_secret in (
            "access-token",
            "refresh-token",
            "controlled-code",
            "state",
            "verifier",
        ):
            self.assertNotIn(controlled_secret, combined_output)


if __name__ == "__main__":
    unittest.main()
