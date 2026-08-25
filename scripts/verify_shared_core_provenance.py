#!/usr/bin/env python3
"""Bind a pinned shared-core rebuild to every embedded copy."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def verify(rebuilt: Path, embedded: list[Path], expected_sha256: str) -> None:
    rebuilt_bytes = rebuilt.read_bytes()
    rebuilt_sha256 = hashlib.sha256(rebuilt_bytes).hexdigest()
    if rebuilt_sha256 != expected_sha256.lower():
        raise ValueError(
            f"rebuilt shared core SHA-256 is {rebuilt_sha256}, expected {expected_sha256}"
        )

    for candidate in embedded:
        if candidate.read_bytes() != rebuilt_bytes:
            raise ValueError(f"embedded shared core differs from rebuild: {candidate}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha256", required=True)
    parser.add_argument("rebuilt", type=Path)
    parser.add_argument("embedded", type=Path, nargs="+")
    args = parser.parse_args()

    try:
        verify(args.rebuilt, args.embedded, args.sha256)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
