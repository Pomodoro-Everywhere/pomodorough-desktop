from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNER = ROOT / "scripts" / "scan_secret.py"


class SecretScannerTests(unittest.TestCase):
    def run_scanner(self, root: Path, secret: str | None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if secret is None:
            environment.pop("COMPROMISED_GOOGLE_CLIENT_SECRET", None)
        else:
            environment["COMPROMISED_GOOGLE_CLIENT_SECRET"] = secret
        return subprocess.run(
            [sys.executable, str(SCANNER), str(root)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_reports_clean_unpacked_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "oauth-client.json").write_text('{"client_secret":""}')

            result = self.run_scanner(Path(directory), "compromised-value")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("secret not found\n", result.stdout)

    def test_fails_when_unpacked_file_contains_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "payload.bin").write_bytes(b"before compromised-value after")

            result = self.run_scanner(Path(directory), "compromised-value")

            self.assertEqual(1, result.returncode)
            self.assertEqual("secret found in unpacked artifact\n", result.stderr)
            self.assertNotIn("compromised-value", result.stderr)

    def test_does_not_disclose_secret_bearing_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "compromised-value"
            secret_directory = Path(directory, secret)
            secret_directory.mkdir()
            Path(secret_directory, secret).write_text(secret)

            result = self.run_scanner(Path(directory), secret)

            self.assertEqual(1, result.returncode)
            self.assertEqual("secret found in unpacked artifact\n", result.stderr)
            self.assertNotIn(secret, result.stderr)

    def test_detects_secret_split_across_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "boundary-secret"
            prefix = b"x" * (1024 * 1024 - 3)
            Path(directory, "payload.bin").write_bytes(prefix + secret.encode())

            result = self.run_scanner(Path(directory), secret)

            self.assertEqual(1, result.returncode)
            self.assertEqual("secret found in unpacked artifact\n", result.stderr)
            self.assertNotIn(secret, result.stderr)

    def test_reports_matches_in_deterministic_path_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Path(root, "z-last.bin").write_text("compromised-value")
            Path(root, "a-first.bin").write_text("compromised-value")

            result = self.run_scanner(root, "compromised-value")

            self.assertEqual(1, result.returncode)
            self.assertEqual("secret found in unpacked artifact\n", result.stderr)

    def test_skips_symlinks_that_escape_the_extraction_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside,
        ):
            target = Path(outside, "outside.bin")
            target.write_text("compromised-value")
            Path(directory, "linked.bin").symlink_to(target)

            result = self.run_scanner(Path(directory), "compromised-value")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("secret not found\n", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are required")
    def test_unreadable_file_fails_closed_without_printing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory, "unreadable.bin")
            payload.write_text("compromised-value")
            payload.chmod(0)
            try:
                result = self.run_scanner(Path(directory), "compromised-value")
            finally:
                payload.chmod(0o600)

            self.assertEqual(2, result.returncode)
            self.assertEqual("could not scan unpacked artifact\n", result.stderr)
            self.assertNotIn("compromised-value", result.stderr)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are required")
    def test_unreadable_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unreadable = Path(directory, "unreadable")
            unreadable.mkdir()
            Path(unreadable, "payload.bin").write_text("compromised-value")
            unreadable.chmod(0)
            try:
                result = self.run_scanner(Path(directory), "compromised-value")
            finally:
                unreadable.chmod(0o700)

            self.assertEqual(2, result.returncode)
            self.assertEqual("could not scan unpacked artifact\n", result.stderr)
            self.assertNotIn("compromised-value", result.stderr)

    def test_rejects_missing_or_empty_secret_and_invalid_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = self.run_scanner(Path(directory), None)
            empty = self.run_scanner(Path(directory), "")
            invalid = self.run_scanner(Path(directory, "missing"), "compromised-value")

        self.assertEqual(2, missing.returncode)
        self.assertEqual(2, empty.returncode)
        self.assertEqual(2, invalid.returncode)


if __name__ == "__main__":
    unittest.main()
