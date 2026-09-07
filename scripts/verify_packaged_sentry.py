#!/usr/bin/env python3
"""Fail closed when a packaged Sentry DSN default is malformed.

Baked-in telemetry defaults flow through OS-specific writers (notably
PowerShell, which can emit a UTF-8 BOM), so every discovered
`sentry_dsn_default` resource is decoded BOM-tolerantly and a non-empty
value must match the Sentry DSN shape. Absence of the resource is allowed:
development builds ship without telemetry.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

RESOURCE_NAME = "sentry_dsn_default"
DSN_FORMAT = re.compile(r"^https://[^@\s]+@[^/\s]+/\d+/?$")


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


def _read_dsn(resource: Path) -> str:
    try:
        return resource.read_bytes().decode("utf-8-sig").strip()
    except (OSError, ValueError):
        raise ValueError(f"unreadable packaged Sentry DSN: {resource}") from None


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {Path(sys.argv[0]).name} PACKAGE_ROOT...", file=sys.stderr)
        return 2
    verified = 0
    for value in sys.argv[1:]:
        root = Path(value)
        if not root.is_dir():
            print("missing package root for Sentry DSN check", file=sys.stderr)
            return 1
        for resource in _resources(root):
            try:
                dsn = _read_dsn(resource)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 1
            if not dsn:
                continue
            if not DSN_FORMAT.match(dsn):
                print(f"invalid packaged Sentry DSN: {resource}", file=sys.stderr)
                return 1
            verified += 1
    if verified:
        print(f"packaged Sentry DSN defaults verified ({verified})")
    else:
        print("no packaged Sentry DSN default (telemetry disabled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
