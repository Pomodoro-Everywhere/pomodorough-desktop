import os
import sys
from pathlib import Path


if os.environ.get("POMODOROUGH_SHARED_CORE_SMOKE") == "1":
    import json
    from importlib.resources import files

    from pomodorough.shared_core import SharedCore

    version = SharedCore().dispatch("core.version", {})
    if not isinstance(version, dict) or version.get("schemaVersion") != 1:
        raise SystemExit("packaged shared core returned an invalid version")
    expected_client_id = os.environ.get("POMODOROUGH_EXPECTED_OAUTH_CLIENT_ID", "")
    oauth_document = json.loads(
        files("pomodorough")
        .joinpath("resources/oauth-client.json")
        .read_text(encoding="utf-8")
    )
    oauth_config = oauth_document.get("installed") or oauth_document
    if (
        not expected_client_id
        or oauth_config.get("client_id") != expected_client_id
        or oauth_config.get("client_secret")
    ):
        raise SystemExit("packaged OAuth resource is invalid")
    compromised_secret = os.environ.get("COMPROMISED_GOOGLE_CLIENT_SECRET", "")
    if not compromised_secret:
        raise SystemExit("compromised-secret scanner is not configured")
    secret_bytes = compromised_secret.encode("utf-8")
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    for payload in bundle_root.rglob("*"):
        if (
            payload.is_file()
            and not payload.is_symlink()
            and secret_bytes in payload.read_bytes()
        ):
            raise SystemExit("compromised secret found in unpacked executable")
    raise SystemExit(0)

from pomodorough.app import main

raise SystemExit(main())
