from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.oauth_artifact_verifier import (
    _restart_in_child,
    main,
    run_platform_store_test,
)


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
