from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pomodorough import oauth_production_signoff as signoff


def windows_acl_failure(
    path: Path, error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
) -> AssertionError:
    diagnostic = error.stderr or "<empty stderr>"
    if isinstance(diagnostic, bytes):
        diagnostic = diagnostic.decode(errors="replace")
    for value in (str(path), os.environ.get("USERPROFILE"),
                  os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")):
        if value:
            diagnostic = re.sub(re.escape(value), "<redacted>", diagnostic,
                                flags=re.IGNORECASE)
    diagnostic = re.sub(r"\bS-\d+(?:-\d+)+\b", "<sid>", diagnostic, flags=re.IGNORECASE)
    if isinstance(error, subprocess.TimeoutExpired):
        outcome = f"timed out after {error.timeout} seconds"
    else:
        outcome = f"failed (exit {error.returncode})"
    return AssertionError(
        f"Windows receipt ACL command {outcome}: {diagnostic.strip()[:2000]}"
    )


def windows_acl_command(path: Path, command: str) -> str:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference = 'Stop'; " + command],
            env={**os.environ, "POMODOROUGH_TEST_ACL_PATH": str(path)},
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise windows_acl_failure(path, error) from None
    return result.stdout


def restrict_windows_receipt_directory(path: Path) -> None:
    username = os.environ.get("USERNAME")
    if not username:
        raise AssertionError("Windows receipt ACL fixture requires USERNAME")
    domain = os.environ.get("USERDOMAIN")
    operator = f"{domain}\\{username}" if domain else username
    try:
        subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r",
             f"{operator}:(OI)(CI)F"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise windows_acl_failure(path, error) from None


def windows_receipt_acl(path: Path) -> dict:
    return json.loads(windows_acl_command(path, """
        $operator = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = [System.IO.File]::GetAccessControl($env:POMODOROUGH_TEST_ACL_PATH)
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


class WindowsReceiptAclFixtureTests(unittest.TestCase):
    def test_fixture_persists_operator_dacl_without_provider_set_acl(self) -> None:
        path = Path("receipt directory's [literal] name")
        environment = {"USERNAME": "private-user", "USERDOMAIN": "private-host"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(subprocess, "run", return_value=SimpleNamespace(stdout="")) as run,
        ):
            restrict_windows_receipt_directory(path)

        arguments = run.call_args.args[0]
        self.assertEqual(arguments, [
            "icacls.exe", str(path), "/inheritance:r", "/grant:r",
            r"private-host\private-user:(OI)(CI)F",
        ])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertTrue(run.call_args.kwargs["text"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_fixture_fails_closed_without_windows_operator(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(subprocess, "run") as run,
            self.assertRaisesRegex(AssertionError, "requires USERNAME"),
        ):
            restrict_windows_receipt_directory(Path("receipt directory"))

        run.assert_not_called()

    def test_fixture_reports_timeout_without_operator_identifiers(self) -> None:
        path = Path("receipt directory's [literal] name")
        environment = {"USERNAME": "private-user", "USERDOMAIN": "private-host"}
        failure = subprocess.TimeoutExpired(
            ["icacls.exe"], 30, stderr=b"PRIVATE-HOST\\PRIVATE-USER timed out",
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(subprocess, "run", side_effect=failure),
            self.assertRaises(AssertionError) as raised,
        ):
            restrict_windows_receipt_directory(path)

        self.assertEqual(
            str(raised.exception),
            "Windows receipt ACL command timed out after 30 seconds: "
            "<redacted>\\<redacted> timed out",
        )
        self.assertTrue(raised.exception.__suppress_context__)

    def test_fixture_reads_file_dacl_without_provider_get_acl(self) -> None:
        path = Path("receipt directory's [literal] name") / "receipt.json"
        expected = {"operator": "operator-sid", "rules": [
            {"sid": "operator-sid", "inherited": True, "access": "Allow", "rights": "FullControl"},
        ]}
        with patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(expected))) as run:
            actual = windows_receipt_acl(path)

        self.assertEqual(actual, expected)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        script = arguments[4]
        self.assertNotIn("get-acl", script.lower())
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn("[System.Security.Principal.WindowsIdentity]::GetCurrent().User", script)
        self.assertIn("[System.IO.File]::GetAccessControl($env:POMODOROUGH_TEST_ACL_PATH)", script)
        self.assertIn("$acl.GetAccessRules($true, $true,", script)
        self.assertIn("[System.Security.Principal.SecurityIdentifier]", script)
        self.assertIn("sid = $_.IdentityReference.Value; inherited = $_.IsInherited", script)
        self.assertIn("access = $_.AccessControlType.ToString(); rights = $_.FileSystemRights.ToString()", script)
        self.assertIn("@{ operator = $operator.Value; rules = $rules }", script)
        self.assertNotIn(str(path), script)
        self.assertEqual(run.call_args.kwargs["env"]["POMODOROUGH_TEST_ACL_PATH"], str(path))
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_command_reports_stderr_without_operator_identifiers_or_stdout(self) -> None:
        path = Path("receipt directory's [literal] name")
        environment = {"USERPROFILE": r"C:\Users\private-user", "USERNAME": "private-user",
                       "USERDOMAIN": "private-host"}
        failure = subprocess.CalledProcessError(
            1, ["powershell.exe"], output="private-stdout",
            stderr=f"Access denied: {str(path).upper()} C:\\Users\\private-user "
                   "PRIVATE-HOST\\PRIVATE-USER S-1-5-21-123-456-789-1001",
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(subprocess, "run", side_effect=failure),
            self.assertRaises(AssertionError) as raised,
        ):
            windows_acl_command(path, "fixture command")

        self.assertEqual(str(raised.exception), "Windows receipt ACL command failed (exit 1): "
                         "Access denied: <redacted> <redacted> <redacted>\\<redacted> <sid>")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_command_reports_empty_stderr(self) -> None:
        for stderr in (None, ""):
            failure = subprocess.CalledProcessError(1, ["powershell.exe"], stderr=stderr)
            with (
                self.subTest(stderr=stderr),
                patch.object(subprocess, "run", side_effect=failure),
                self.assertRaisesRegex(AssertionError, "exit 1.*<empty stderr>"),
            ):
                windows_acl_command(Path("receipt.json"), "fixture command")


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
