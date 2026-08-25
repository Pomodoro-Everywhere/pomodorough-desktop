from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "scripts" / "verify_packaged_oauth.py"


class PackagedOAuthValidationTests(unittest.TestCase):
    def run_validator(
        self,
        expected: dict[str, object],
        packaged: list[dict[str, object] | None],
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected_path = root / "expected.json"
            expected_path.write_text(json.dumps(expected), encoding="utf-8")
            package_roots: list[Path] = []
            for index, document in enumerate(packaged):
                package_root = root / f"package-{index}"
                package_root.mkdir()
                package_roots.append(package_root)
                if document is not None:
                    resource = package_root / "nested" / "oauth-client.json"
                    resource.parent.mkdir()
                    resource.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    str(expected_path),
                    *(str(path) for path in package_roots),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

    def test_accepts_final_secret_free_client_in_every_package(self) -> None:
        document = {"installed": {"client_id": "final-client"}}
        result = self.run_validator(document, [document, document, document, document])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("packaged OAuth resources verified", result.stdout)

    def test_rejects_missing_package_resource(self) -> None:
        document = {"installed": {"client_id": "final-client"}}
        result = self.run_validator(document, [document, None])

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing packaged OAuth resource", result.stderr)

    def test_rejects_wrong_client_or_nonempty_secret(self) -> None:
        expected = {"installed": {"client_id": "final-client"}}
        wrong_client = {"installed": {"client_id": "old-client"}}
        leaked_secret = {
            "installed": {
                "client_id": "final-client",
                "client_secret": "must-not-ship",
            }
        }

        wrong_result = self.run_validator(expected, [wrong_client])
        secret_result = self.run_validator(expected, [leaked_secret])

        self.assertEqual(wrong_result.returncode, 1)
        self.assertEqual(secret_result.returncode, 1)
        self.assertIn("invalid packaged OAuth resource", wrong_result.stderr)
        self.assertIn("invalid packaged OAuth resource", secret_result.stderr)

    def test_rejects_changed_authorization_or_token_endpoint(self) -> None:
        expected = {
            "installed": {
                "client_id": "final-client",
                "auth_uri": "https://accounts.example/authorize",
                "token_uri": "https://accounts.example/token",
            }
        }
        changed_authorization = {
            "installed": {
                "client_id": "final-client",
                "auth_uri": "https://attacker.example/authorize",
                "token_uri": "https://accounts.example/token",
            }
        }
        changed_token = {
            "installed": {
                "client_id": "final-client",
                "auth_uri": "https://accounts.example/authorize",
                "token_uri": "https://attacker.example/token",
            }
        }

        authorization_result = self.run_validator(expected, [changed_authorization])
        token_result = self.run_validator(expected, [changed_token])

        self.assertEqual(authorization_result.returncode, 1)
        self.assertEqual(token_result.returncode, 1)
        self.assertIn("invalid packaged OAuth resource", authorization_result.stderr)
        self.assertIn("invalid packaged OAuth resource", token_result.stderr)


if __name__ == "__main__":
    unittest.main()
