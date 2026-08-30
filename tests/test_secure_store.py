from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.secure_store import (
    PlatformSecretStore,
    SecretMutationJournal,
    SecureStoreError,
)


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
            self.assertRaisesRegex(SecureStoreError, "malformed data"),
        ):
            self.store.load("endpoint-key")

    def test_missing_platform_value_returns_none(self) -> None:
        response = subprocess.CompletedProcess([], 1, "", "")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response),
        ):
            self.assertIsNone(self.store.load("endpoint-key"))

    def test_failed_journal_lookup_aborts_before_mutating_existing_secret(self) -> None:
        response = subprocess.CompletedProcess([], 1, "", "keyring is locked")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response) as run,
            self.assertRaisesRegex(SecureStoreError, "keyring is locked"),
            SecretMutationJournal(self.store) as journal,
        ):
            journal.save("existing-key", b"replacement")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:2], ["secret-tool", "lookup"])

    def test_rejected_save_preserves_platform_error(self) -> None:
        response = subprocess.CompletedProcess([], 1, "", "keyring is locked")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response),
            self.assertRaisesRegex(SecureStoreError, "keyring is locked"),
        ):
            self.store.save("endpoint-key", b"secret")

    def test_rejected_delete_preserves_platform_error(self) -> None:
        response = subprocess.CompletedProcess([], 1, "", "keyring is locked")
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value="/bin/secret-tool"),
            patch("pomodorough.secure_store.subprocess.run", return_value=response),
            self.assertRaisesRegex(SecureStoreError, "keyring is locked"),
        ):
            self.store.delete("endpoint-key")

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

    def test_unavailable_store_rejects_delete_without_spawning_process(self) -> None:
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch("pomodorough.secure_store.shutil.which", return_value=None),
            patch("pomodorough.secure_store.subprocess.run") as run,
            self.assertRaisesRegex(SecureStoreError, "secret-tool was not found"),
        ):
            self.store.delete("endpoint-key")

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
        ), self.assertRaisesRegex(SecureStoreError, "Platform secure storage failed"):
            self.store._run(["secret-tool", "lookup"])

    def test_macos_save_uses_in_process_keychain_api(self) -> None:
        with (
            patch("pomodorough.secure_store.os.name", "posix"),
            patch("pomodorough.secure_store.sys_platform", return_value="darwin"),
            patch.object(self.store, "availability", return_value=(True, "ready")),
            patch("pomodorough.secure_store._macos_save") as save,
            patch("pomodorough.secure_store.subprocess.run") as run,
        ):
            self.store.save("endpoint-key", b"secret")

        save.assert_called_once_with(
            self.store.service, "endpoint-key", b"secret"
        )
        run.assert_not_called()

    def test_interleaved_lookup_cannot_redirect_another_secret_save(self) -> None:
        def availability():
            self.store.load("active-session")
            return True, "ready"

        with (
            patch("pomodorough.secure_store.sys_platform", return_value="linux"),
            patch.object(self.store, "availability", side_effect=availability),
            patch.object(self.store, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
        ):
            self.store.save("pending-revocations", b"captured-session")

        self.assertEqual(run.call_args_list[0].args[0][-1], "active-session")
        self.assertEqual(run.call_args_list[1].args[0][-1], "pending-revocations")


class MemorySecretStore:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = dict(values)
        self.fail_save_key: str | None = None

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        if key == self.fail_save_key:
            raise OSError("restore failed")
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class SecretMutationJournalTests(unittest.TestCase):
    def test_successful_journal_commits_external_mutations(self) -> None:
        store = MemorySecretStore({"existing": b"before", "deleted": b"remove"})

        with SecretMutationJournal(store) as journal:
            journal.save("existing", b"after")
            journal.save("created", b"new")
            journal.delete("deleted")

        self.assertEqual(store.values, {"existing": b"after", "created": b"new"})

    def test_failed_transaction_restores_original_values_and_removes_new_keys(self) -> None:
        original = {"existing": b"before", "deleted": b"remove"}
        store = MemorySecretStore(original)

        with (
            self.assertRaisesRegex(RuntimeError, "transaction failed"),
            SecretMutationJournal(store) as journal,
        ):
            journal.save("existing", b"first")
            journal.save("existing", b"second")
            journal.save("created", b"new")
            journal.delete("deleted")
            raise RuntimeError("transaction failed")

        self.assertEqual(store.values, original)

    def test_rollback_failure_is_explicit_and_preserves_its_cause(self) -> None:
        store = MemorySecretStore({"existing": b"before"})

        with (
            self.assertRaisesRegex(SecureStoreError, "rollback failed") as raised,
            SecretMutationJournal(store) as journal,
        ):
            journal.save("existing", b"after")
            store.fail_save_key = "existing"
            raise RuntimeError("transaction failed")

        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual(store.values["existing"], b"after")


if __name__ == "__main__":
    unittest.main()
