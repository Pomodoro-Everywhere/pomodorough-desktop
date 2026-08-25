from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.secure_store import PlatformSecretStore, SecureStoreError


class PlatformSecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = PlatformSecretStore(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_linux_store_roundtrip_uses_scoped_attributes_and_base64(self) -> None:
        encoded = base64.b64encode(b"room capability").decode("ascii")
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, encoded + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
            patch("pomodorough.secure_store.subprocess.run", side_effect=responses) as run,
        ):
            self.store.save("room:alpha", b"room capability")
            loaded = self.store.load("room:alpha")
            self.store.delete("room:alpha")

        self.assertEqual(loaded, b"room capability")
        calls = run.call_args_list
        self.assertEqual(calls[0].kwargs["input"], encoded)
        self.assertEqual(calls[1].args[0][:2], ["secret-tool", "lookup"])
        self.assertEqual(calls[2].args[0][:2], ["secret-tool", "clear"])
        self.assertIn("room:alpha", calls[0].args[0])

    def test_malformed_platform_value_fails_closed(self) -> None:
        response = subprocess.CompletedProcess([], 0, "not base64!", "")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response),
        ):
            with self.assertRaisesRegex(SecureStoreError, "malformed data"):
                self.store.load("endpoint-key")

    def test_rejected_save_preserves_platform_error(self) -> None:
        response = subprocess.CompletedProcess([], 1, "", "keyring is locked")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response),
        ):
            with self.assertRaisesRegex(SecureStoreError, "keyring is locked"):
                self.store.save("endpoint-key", b"secret")

    def test_unavailable_store_rejects_save_without_spawning_process(self) -> None:
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value=None),
            patch("pomodorough.secure_store.subprocess.run") as run,
        ):
            available, reason = self.store.availability()
            with self.assertRaisesRegex(SecureStoreError, "secret-tool was not found"):
                self.store.save("endpoint-key", b"secret")

        self.assertFalse(available)
        self.assertIn("secret-tool", reason)
        run.assert_not_called()

    def test_invalid_keys_and_values_fail_before_platform_access(self) -> None:
        with patch("pomodorough.secure_store.subprocess.run") as run:
            for key in ("", b"bytes", "x" * 513):
                with self.subTest(key=key), self.assertRaises(SecureStoreError):
                    self.store.load(key)  # type: ignore[arg-type]
            for value in (b"", "text"):
                with self.subTest(value=value), self.assertRaises(SecureStoreError):
                    self.store.save("valid", value)  # type: ignore[arg-type]
        run.assert_not_called()

    def test_private_write_replaces_value_with_owner_only_permissions(self) -> None:
        path = self.root / "nested" / "credential"
        PlatformSecretStore._write_private(path, b"first")
        PlatformSecretStore._write_private(path, b"second")

        self.assertEqual(path.read_bytes(), b"second")
        if os.name != "nt" and hasattr(os, "fchmod"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_process_failures_are_reported_as_secure_store_errors(self) -> None:
        with patch(
            "pomodorough.secure_store.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["secret-tool"], 15),
        ):
            with self.assertRaisesRegex(SecureStoreError, "Platform secure storage failed"):
                self.store._run(["secret-tool", "lookup"])

    def test_macos_save_passes_secret_as_keychain_argument_not_stdin(self) -> None:
        response = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="darwin"),
            patch("pomodorough.secure_store.shutil.which", return_value="/usr/bin/security"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response) as run,
        ):
            self.store.save("endpoint-key", b"secret")

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["security", "add-generic-password"])
        self.assertEqual(command[-1], base64.b64encode(b"secret").decode("ascii"))
        self.assertIsNone(run.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
