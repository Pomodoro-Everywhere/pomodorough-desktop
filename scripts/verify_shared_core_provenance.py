#!/usr/bin/env python3
"""Verify a pinned rebuild and canonical embedded shared-core copies."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def verify(rebuilt: Path, embedded: list[Path], expected_sha256: str) -> None:
    rebuilt_bytes = rebuilt.read_bytes()
    if not rebuilt_bytes.startswith(b"\0asm\x01\0\0\0"):
        raise ValueError("rebuilt shared core is not a WebAssembly module")

    canonical_bytes: bytes | None = None
    for candidate in embedded:
        candidate_bytes = candidate.read_bytes()
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        if candidate_sha256 != expected_sha256.lower():
            raise ValueError(
                f"embedded shared core SHA-256 is {candidate_sha256}, "
                f"expected {expected_sha256}: {candidate}"
            )
        if canonical_bytes is None:
            canonical_bytes = candidate_bytes
        elif candidate_bytes != canonical_bytes:
            raise ValueError(f"embedded shared core copies differ: {candidate}")


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
