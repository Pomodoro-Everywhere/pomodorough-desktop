from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough import oauth_production_signoff as signoff


def windows_acl_command(path: Path, command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference = 'Stop'; " + command],
        env={**os.environ, "POMODOROUGH_TEST_ACL_PATH": str(path)},
        capture_output=True, text=True, check=True, timeout=30,
    )
    return result.stdout


def restrict_windows_receipt_directory(path: Path) -> None:
    windows_acl_command(path, """
        $operator = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $operator, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $env:POMODOROUGH_TEST_ACL_PATH -AclObject $acl
    """)


def windows_receipt_acl(path: Path) -> dict:
    return json.loads(windows_acl_command(path, """
        $operator = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $env:POMODOROUGH_TEST_ACL_PATH
        $rules = @($acl.GetAccessRules($true, $true,
            [System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
            @{ sid = $_.IdentityReference.Value; inherited = $_.IsInherited;
               access = $_.AccessControlType.ToString(); rights = $_.FileSystemRights.ToString() }
        })
        @{ operator = $operator.Value; rules = $rules } | ConvertTo-Json -Depth 4 -Compress
    """))


class _SecretStore:
    def __init__(self) -> None:
        self.value: bytes | None = b"stored-refresh"

    def load(self, _key: str) -> bytes | None:
        return self.value


class _TokenStore:
    def __init__(self) -> None:
        self.secret_store = _SecretStore()
        self.secret_key = "oauth:test-device"


class _Accounts:
    def __init__(self, store: _TokenStore) -> None:
        self.store = store
        self.signed_out = False
        self.revoked = False

    def sign_out(self) -> object:
        self.signed_out = True
        self.store.secret_store.value = None
        return object()

    def revoke(self, _state: object) -> None:
        self.revoked = True


class _Service:
    def __init__(self) -> None:
        self.access_token = "access"
        self.refresh_token = "refresh"
        self.token_store = _TokenStore()
        self._accounts = _Accounts(self.token_store)
        self.shutdown_called = False

    def _authorize_google(self) -> dict[str, str]:
        return {"id": "private-account", "email": "private@example.test"}

    def shutdown(self) -> None:
        self.shutdown_called = True


class ProductionOAuthSignoffTests(unittest.TestCase):
    def test_receipt_hashes_account_and_excludes_private_profile(self) -> None:
        fingerprint = signoff._account_fingerprint(
            {"id": "private-account", "email": "private@example.test"}
        )
        receipt = signoff._receipt("wheel", "a" * 64, "public-client", fingerprint)
        encoded = json.dumps(receipt)

        self.assertTrue(fingerprint.startswith("sha256:"))
        self.assertNotIn("private-account", encoded)
        self.assertNotIn("private@example.test", encoded)
        self.assertNotIn("access", encoded)
        self.assertNotIn("refresh", encoded)

    def test_success_proves_restart_then_cleans_both_session_copies(self) -> None:
        service = _Service()
        with (
            patch.object(signoff, "_new_service", return_value=service),
            patch.object(signoff, "_restart_in_child", return_value=True) as restart,
        ):
            fingerprint = signoff._execute_signoff(Path("/tmp/signoff"), "device")

        restart.assert_called_once_with(Path("/tmp/signoff"), "device", fingerprint)
        self.assertTrue(service._accounts.signed_out)
        self.assertTrue(service._accounts.revoked)
        self.assertTrue(service.shutdown_called)

    def test_restart_failure_still_removes_and_revokes_credentials(self) -> None:
        service = _Service()
        with (
            patch.object(signoff, "_new_service", return_value=service),
            patch.object(signoff, "_restart_in_child", return_value=False),
            self.assertRaisesRegex(signoff.ProductionSignoffError, "restoration"),
        ):
            signoff._execute_signoff(Path("/tmp/signoff"), "device")

        self.assertTrue(service._accounts.signed_out)
        self.assertTrue(service._accounts.revoked)
        self.assertTrue(service.shutdown_called)

    def test_sign_in_failure_still_removes_and_revokes_partial_session(self) -> None:
        service = _Service()
        with (
            patch.object(signoff, "_new_service", return_value=service),
            patch.object(service, "_authorize_google", side_effect=RuntimeError),
            self.assertRaisesRegex(signoff.ProductionSignoffError, "sign-in"),
        ):
            signoff._execute_signoff(Path("/tmp/signoff"), "device")

        self.assertTrue(service._accounts.signed_out)
        self.assertTrue(service._accounts.revoked)
        self.assertTrue(service.shutdown_called)

    def test_packaged_credentials_reject_active_override(self) -> None:
        packaged = {"client_id": "packaged", "client_secret": ""}
        active = {"client_id": "override", "client_secret": ""}
        with (
            patch.object(signoff, "_parse_oauth_credentials", return_value=packaged),
            patch.object(signoff, "_read_oauth_credentials", return_value=active),
            self.assertRaisesRegex(signoff.ProductionSignoffError, "differs"),
        ):
            signoff._packaged_oauth_credentials()

    def test_run_rejects_invalid_digest_before_sign_in(self) -> None:
        with (
            patch.object(signoff, "_execute_signoff") as execute,
            self.assertRaisesRegex(signoff.ProductionSignoffError, "SHA-256"),
        ):
            signoff.run_production_signoff("wheel", "not-a-digest")

        execute.assert_not_called()

    def test_receipt_file_is_private_and_never_overwritten(self) -> None:
        receipt = {"schemaVersion": 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            if os.name == "nt":
                restrict_windows_receipt_directory(path.parent)
            open_file = Mock(wraps=os.open)
            receipt_os = SimpleNamespace(**{**vars(os), "open": open_file})
            with patch.object(signoff, "os", receipt_os):
                signoff._write_receipt(receipt, str(path))
            open_file.assert_called_once_with(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

            self.assertEqual(json.loads(path.read_text()), receipt)
            if os.name == "nt":
                acl = windows_receipt_acl(path)
                self.assertTrue(acl["rules"])
                for rule in acl["rules"]:
                    self.assertEqual(rule, {"sid": acl["operator"], "inherited": True,
                                            "access": "Allow", "rights": "FullControl"})
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                signoff._write_receipt({"schemaVersion": 2}, str(path))
            self.assertEqual(path.read_bytes(), original)

    def test_child_environment_drops_sensitive_operator_inputs(self) -> None:
        with patch.dict(
            os.environ,
            {
                "COMPROMISED_GOOGLE_CLIENT_SECRET": "secret",
                "POMODOROUGH_GOOGLE_OAUTH_JSON": "/tmp/override.json",
            },
        ):
            environment = signoff._child_environment()

        self.assertNotIn("COMPROMISED_GOOGLE_CLIENT_SECRET", environment)
        self.assertNotIn("POMODOROUGH_GOOGLE_OAUTH_JSON", environment)


if __name__ == "__main__":
    unittest.main()
