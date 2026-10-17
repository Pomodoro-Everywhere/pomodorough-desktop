"""R49-D02 residual: provider-unavailable save/load/delete matrix."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from pomodorough.secure_store import (
    PlatformSecretStore,
    SecureStoreError,
    is_infra_secure_store_error,
)
from test_secure_store import linux_secret_store


def _failing_tool(tmp_path, message="org.freedesktop.secrets unavailable"):
    executable = tmp_path / "secret-tool"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({message!r}, file=sys.stderr)\nsys.exit(1)\n"
    )
    executable.chmod(0o700)
    return executable


class ProviderUnavailableMatrixTests(unittest.TestCase):
    def test_save_load_delete_share_actionable_provider_error(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            _failing_tool(tmp)
            with linux_secret_store(tmp), patch.dict(os.environ, {"PATH": str(tmp)}):
                store = PlatformSecretStore(tmp)
                available, reason = store.availability()
                self.assertFalse(available)
                self.assertIn("org.freedesktop.secrets", reason)
                self.assertIn("GNOME Keyring", reason)
                for operation in (
                    lambda: store.load("k"),
                    lambda: store.save("k", b"v"),
                    lambda: store.delete("k"),
                ):
                    with self.assertRaises(SecureStoreError) as ctx:
                        operation()
                    self.assertIn("Start and unlock", str(ctx.exception))
                    self.assertTrue(is_infra_secure_store_error(ctx.exception))
                    cause = ctx.exception.__cause__
                    self.assertIsInstance(
                        cause, subprocess.CalledProcessError
                    )

    def test_empty_store_stays_distinct_from_provider_failure(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            exe = tmp / "secret-tool"
            exe.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
            exe.chmod(0o700)
            with linux_secret_store(tmp), patch.dict(os.environ, {"PATH": str(tmp)}):
                store = PlatformSecretStore(tmp)
                self.assertTrue(store.availability()[0])
                self.assertIsNone(store.load("missing"))

    def test_timeout_matrix_is_actionable_on_all_paths(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            exe = tmp / "secret-tool"
            exe.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
            exe.chmod(0o700)
            with linux_secret_store(tmp), patch.dict(os.environ, {"PATH": str(tmp)}), patch(
                "pomodorough.secure_store.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["secret-tool"], 15),
            ):
                store = PlatformSecretStore(tmp)
                self.assertFalse(store.availability()[0])
                for operation in (
                    lambda: store.load("k"),
                    lambda: store.save("k", b"v"),
                    lambda: store.delete("k"),
                ):
                    with self.assertRaises(SecureStoreError) as ctx:
                        operation()
                    self.assertIn("Start and unlock", str(ctx.exception))
                    self.assertTrue(is_infra_secure_store_error(ctx.exception))


if __name__ == "__main__":
    unittest.main()
