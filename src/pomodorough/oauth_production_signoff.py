from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication

from . import __version__
from .network import (
    API_BASE,
    CloudService,
    TokenStore,
    _parse_oauth_credentials,
    _read_oauth_credentials,
)
from .network_account import RevocationState


_DIGEST_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_ARTIFACTS = ("wheel", "flatpak", "windows")


class ProductionSignoffError(RuntimeError):
    pass


def _account_fingerprint(user: dict[str, Any]) -> str:
    account_id = user.get("id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ProductionSignoffError("production account response is invalid")
    material = f"pomodorough-oauth-signoff-v1\0{account_id}".encode()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _packaged_oauth_credentials() -> dict[str, str]:
    bundled = files("pomodorough").joinpath("resources/oauth-client.json")
    packaged = _parse_oauth_credentials(bundled)
    if not packaged.get("client_secret"):
        raise ProductionSignoffError("packaged OAuth client is missing its secret")
    if _read_oauth_credentials() != packaged:
        raise ProductionSignoffError("active OAuth config differs from packaged config")
    return packaged


def _new_service(device_id: str, root: Path) -> CloudService:
    store = TokenStore(device_id, fallback_path=root / "session-tombstone.json")
    return CloudService(device_id, API_BASE, token_store=store)


def _restart_command(root: Path, device_id: str, fingerprint: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "--oauth-production-restart-child",
            str(root),
            device_id,
            fingerprint,
        ]
    return [
        sys.executable,
        "-m",
        "pomodorough.oauth_production_signoff",
        "--restart-child",
        str(root),
        device_id,
        fingerprint,
    ]


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("COMPROMISED_GOOGLE_CLIENT_SECRET", None)
    environment.pop("POMODOROUGH_GOOGLE_OAUTH_JSON", None)
    return environment


def _restart_in_child(root: Path, device_id: str, fingerprint: str) -> bool:
    try:
        result = subprocess.run(
            _restart_command(root, device_id, fingerprint),
            capture_output=True,
            check=False,
            env=_child_environment(),
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _verify_restored_process(
    root: Path, device_id: str, expected_fingerprint: str
) -> bool:
    service = _new_service(device_id, root)
    try:
        response = service._authorized_request("GET", "/api/v1/me")
        fingerprint = _account_fingerprint(response.get("user", {}))
        return secrets.compare_digest(fingerprint, expected_fingerprint)
    except Exception:  # noqa: BLE001 - child never emits credential-bearing errors
        return False
    finally:
        service.shutdown()


def _local_credentials_removed(store: TokenStore) -> bool:
    if store.secret_store is None:
        return store.load() is None
    try:
        return store.secret_store.load(store.secret_key) is None
    except Exception:  # noqa: BLE001 - cleanup must continue to server revocation
        return False


def _cleanup(service: CloudService) -> tuple[bool, bool]:
    captured = RevocationState(
        service.access_token,
        service.refresh_token,
        bool(service.access_token),
    )
    local_removed = False
    try:
        service._accounts.sign_out()
        local_removed = _local_credentials_removed(service.token_store)
    except Exception:  # noqa: BLE001 - cleanup must continue to server revocation
        pass
    revoked = False
    try:
        if captured.access_token or captured.refresh_token:
            service._accounts.revoke(captured)
            revoked = True
    except Exception:  # noqa: BLE001 - caller reports only sanitized failure
        pass
    return local_removed, revoked


def _execute_signoff(root: Path, device_id: str) -> str:
    service = _new_service(device_id, root)
    failure: str | None = None
    fingerprint = ""
    try:
        user = service._authorize_google()
        fingerprint = _account_fingerprint(user)
        if not _restart_in_child(root, device_id, fingerprint):
            failure = "secure restoration failed"
    except Exception:  # noqa: BLE001 - credential-bearing errors stay private
        failure = "production sign-in failed"
    finally:
        try:
            local_removed, revoked = _cleanup(service)
        finally:
            service.shutdown()
    if failure is not None:
        raise ProductionSignoffError(failure)
    if not local_removed:
        raise ProductionSignoffError("local credential removal failed")
    if not revoked:
        raise ProductionSignoffError("server logout failed")
    return fingerprint


def _receipt(
    artifact: str,
    asset_sha256: str,
    client_id: str,
    account_fingerprint: str,
) -> dict[str, Any]:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schemaVersion": 1,
        "artifact": artifact,
        "artifactVersion": __version__,
        "releaseTag": f"v{__version__}",
        "assetSha256": asset_sha256,
        "completedAt": completed_at.replace("+00:00", "Z"),
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "production": {
            "apiBase": API_BASE,
            "oauthClientId": client_id,
            "accountFingerprint": account_fingerprint,
        },
        "checks": {
            "googleSignIn": True,
            "productionAccount": True,
            "secureRestoration": True,
            "serverLogout": True,
            "localCredentialRemoval": True,
        },
    }


def run_production_signoff(artifact: str, asset_sha256: str) -> dict[str, Any]:
    if artifact not in _ARTIFACTS:
        raise ProductionSignoffError("unsupported artifact")
    if not _DIGEST_PATTERN.fullmatch(asset_sha256):
        raise ProductionSignoffError("asset SHA-256 is invalid")
    QCoreApplication.instance() or QCoreApplication([])
    credentials = _packaged_oauth_credentials()
    device_id = f"oauth-release-signoff-{secrets.token_hex(16)}"
    with tempfile.TemporaryDirectory(prefix="pomodorough-oauth-signoff-") as directory:
        fingerprint = _execute_signoff(Path(directory), device_id)
    return _receipt(
        artifact,
        asset_sha256.lower(),
        credentials["client_id"],
        fingerprint,
    )


def _write_receipt(receipt: dict[str, Any], destination: str) -> None:
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if destination == "-":
        sys.stdout.write(encoded)
        return
    descriptor = os.open(
        Path(destination),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(encoded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--artifact", choices=_ARTIFACTS)
    modes.add_argument("--restart-child", nargs=3, metavar=("ROOT", "DEVICE", "HASH"))
    parser.add_argument("--asset-sha256")
    parser.add_argument("--receipt")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.restart_child is not None:
        root, device_id, fingerprint = args.restart_child
        passed = _verify_restored_process(Path(root), device_id, fingerprint)
        return 0 if passed else 1
    if not args.asset_sha256 or not args.receipt:
        parser.error("--asset-sha256 and --receipt are required")
    try:
        receipt = run_production_signoff(args.artifact, args.asset_sha256)
        _write_receipt(receipt, args.receipt)
    except Exception:  # noqa: BLE001 - command boundary suppresses private errors
        print("production OAuth sign-off failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
