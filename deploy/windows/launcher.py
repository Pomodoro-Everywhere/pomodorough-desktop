import os


if os.environ.get("POMODOROUGH_SHARED_CORE_SMOKE") == "1":
    from pomodorough.shared_core import SharedCore

    version = SharedCore().dispatch("core.version", {})
    if not isinstance(version, dict) or version.get("schemaVersion") != 1:
        raise SystemExit("packaged shared core returned an invalid version")
    raise SystemExit(0)

from pomodorough.app import main

raise SystemExit(main())
