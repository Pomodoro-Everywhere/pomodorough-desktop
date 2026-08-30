import os
import sys


if len(sys.argv) == 3 and sys.argv[1] == "--oauth-verifier-restart-child":
    from pomodorough.oauth_artifact_verifier import main

    raise SystemExit(main(["--restart-child", sys.argv[2]]))


if len(sys.argv) == 5 and sys.argv[1] == "--platform-store-verifier-child":
    from pomodorough.oauth_artifact_verifier import main

    raise SystemExit(main(["--platform-store-child", *sys.argv[2:]]))


if len(sys.argv) == 5 and sys.argv[1] == "--oauth-production-restart-child":
    from pomodorough.oauth_production_signoff import main

    raise SystemExit(main(["--restart-child", *sys.argv[2:]]))


if os.environ.get("POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST") == "1":
    from pomodorough.oauth_artifact_verifier import main

    raise SystemExit(main(["--self-test"]))


if os.environ.get("POMODOROUGH_PLATFORM_STORE_SELF_TEST") == "1":
    from pomodorough.oauth_artifact_verifier import main

    raise SystemExit(main(["--platform-store-self-test"]))


if os.environ.get("POMODOROUGH_OAUTH_PRODUCTION_SIGNOFF") == "1":
    from pomodorough.oauth_production_signoff import main

    receipt = os.environ.get("POMODOROUGH_OAUTH_SIGNOFF_RECEIPT", "")
    digest = os.environ.get("POMODOROUGH_OAUTH_ASSET_SHA256", "")
    if not receipt or receipt == "-" or not digest:
        raise SystemExit("production OAuth sign-off configuration is incomplete")
    raise SystemExit(
        main(
            [
                "--artifact",
                "windows",
                "--asset-sha256",
                digest,
                "--receipt",
                receipt,
            ]
        )
    )


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
    raise SystemExit(0)

from pomodorough.app import main

raise SystemExit(main())
