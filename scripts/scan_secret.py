from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
SECRET_VARIABLE = "COMPROMISED_GOOGLE_CLIENT_SECRET"


def contains_secret(path: Path, secret: bytes) -> bool:
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            candidate = overlap + chunk
            if secret in candidate:
                return True
            overlap = candidate[-(len(secret) - 1) :] if len(secret) > 1 else b""
    return False


def _raise_walk_error(error: OSError) -> None:
    raise error


def scan(root: Path, secret: bytes) -> list[Path]:
    matches: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=_raise_walk_error,
    ):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not stat.S_ISLNK((directory_path / name).lstat().st_mode)
        )
        for name in sorted(file_names):
            path = directory_path / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                continue
            if contains_secret(path, secret):
                matches.append(path.relative_to(root))
    return matches


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} ROOT", file=sys.stderr)
        return 2
    value = os.environ.get(SECRET_VARIABLE, "")
    if not value:
        print(f"{SECRET_VARIABLE} is missing or empty", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print("scan root is not a directory", file=sys.stderr)
        return 2
    try:
        matches = scan(root, value.encode("utf-8"))
    except OSError:
        print("could not scan unpacked artifact", file=sys.stderr)
        return 2
    if matches:
        print("secret found in unpacked artifact", file=sys.stderr)
        return 1
    print("secret not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
