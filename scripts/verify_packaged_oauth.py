from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


RESOURCE_NAME = "oauth-client.json"


def _oauth_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("OAuth document must be an object")
    config = document.get("installed") or document.get("web") or document
    if not isinstance(config, dict):
        raise ValueError("OAuth configuration must be an object")
    return config


def _load_config(path: Path) -> dict[str, Any]:
    return _oauth_config(json.loads(path.read_text(encoding="utf-8")))


def _resources(root: Path) -> list[Path]:
    resources: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not stat.S_ISLNK((directory_path / name).lstat().st_mode)
        )
        if RESOURCE_NAME not in file_names:
            continue
        candidate = directory_path / RESOURCE_NAME
        if stat.S_ISREG(candidate.lstat().st_mode):
            resources.append(candidate)
    return resources


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f"usage: {Path(sys.argv[0]).name} EXPECTED_JSON PACKAGE_ROOT...",
            file=sys.stderr,
        )
        return 2
    try:
        expected = _load_config(Path(sys.argv[1]))
    except (OSError, ValueError, json.JSONDecodeError):
        print("invalid expected OAuth resource", file=sys.stderr)
        return 2
    expected_client_id = expected.get("client_id")
    if not isinstance(expected_client_id, str) or not expected_client_id:
        print("invalid expected OAuth resource", file=sys.stderr)
        return 2
    if expected.get("client_secret", ""):
        print("expected OAuth resource contains a client secret", file=sys.stderr)
        return 2

    verified = 0
    for value in sys.argv[2:]:
        root = Path(value)
        if not root.is_dir():
            print("missing packaged OAuth resource", file=sys.stderr)
            return 1
        resources = _resources(root)
        if not resources:
            print("missing packaged OAuth resource", file=sys.stderr)
            return 1
        for resource in resources:
            try:
                packaged = _load_config(resource)
            except (OSError, ValueError, json.JSONDecodeError):
                print("invalid packaged OAuth resource", file=sys.stderr)
                return 1
            if packaged != expected or packaged.get("client_secret", ""):
                print("invalid packaged OAuth resource", file=sys.stderr)
                return 1
            verified += 1

    print(f"packaged OAuth resources verified ({verified})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
