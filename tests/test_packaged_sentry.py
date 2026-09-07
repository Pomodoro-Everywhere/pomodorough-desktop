from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "scripts" / "verify_packaged_sentry.py"
DSN = "https://public@example.ingest.sentry.io/1"


class PackagedSentryValidationTests(unittest.TestCase):
    def run_validator(
        self,
        packaged: list[bytes | None],
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_roots: list[Path] = []
            for index, payload in enumerate(packaged):
                package_root = root / f"package-{index}"
                package_root.mkdir()
                package_roots.append(package_root)
                if payload is not None:
                    resource = package_root / "nested" / "sentry_dsn_default"
                    resource.parent.mkdir()
                    resource.write_bytes(payload)
            return subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    *(str(path) for path in package_roots),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

    def test_accepts_valid_dsn_in_every_package(self) -> None:
        payload = DSN.encode("utf-8")
        result = self.run_validator([payload, payload, payload, payload])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("packaged Sentry DSN defaults verified (4)", result.stdout)

    def test_accepts_bom_prefixed_dsn(self) -> None:
        payload = b"\xef\xbb\xbf" + DSN.encode("utf-8")
        result = self.run_validator([payload])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("packaged Sentry DSN defaults verified (1)", result.stdout)

    def test_accepts_missing_resource_everywhere(self) -> None:
        result = self.run_validator([None, None])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("telemetry disabled", result.stdout)

    def test_accepts_empty_file_as_disabled(self) -> None:
        result = self.run_validator([b"  \n"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("telemetry disabled", result.stdout)

    def test_rejects_malformed_dsn(self) -> None:
        for payload in (
            b"not-a-dsn",
            b"https://public@example.ingest.sentry.io",
            b"http://public@example.ingest.sentry.io/1",
            b"\xef\xbb\xbfbogus",
        ):
            with self.subTest(payload=payload):
                result = self.run_validator([payload])

                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid packaged Sentry DSN", result.stderr)

    def test_rejects_missing_package_root(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), "/nonexistent-root"],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing package root", result.stderr)


if __name__ == "__main__":
    unittest.main()
