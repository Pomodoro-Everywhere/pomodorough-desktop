#!/usr/bin/env python3
"""Fail CI when a repository-relative Markdown link points to a missing path."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
RETIRED_GITHUB_NAMESPACE = re.compile(
    r"https?://github\.com/egigoka(?:/|$)", re.IGNORECASE
)
IGNORED_PREFIXES = ("http://", "https://", "mailto:", "#", "data:")
IGNORED_PARTS = {".git", "node_modules", "dist", "build", ".build", ".gradle", "DerivedData"}


def tracked_paths(root: Path) -> set[Path]:
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {(root / path).resolve() for path in output.split("\0") if path}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tracked = tracked_paths(root)
    failures: list[str] = []
    checked = 0
    for document in sorted(path for path in tracked if path.suffix.lower() == ".md"):
        if any(part in IGNORED_PARTS for part in document.relative_to(root).parts):
            continue
        text = document.read_text(encoding="utf-8")
        for match in RETIRED_GITHUB_NAMESPACE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(
                f"{document.relative_to(root)}:{line}: retired GitHub namespace: {match.group(0)}"
            )
        for match in LINK.finditer(text):
            raw = match.group(1).strip()
            if raw.startswith(IGNORED_PREFIXES):
                continue
            if raw.startswith("<") and ">" in raw:
                raw = raw[1:raw.index(">")]
            elif " " in raw:
                raw = raw.split(" ", 1)[0]
            target = unquote(raw.split("#", 1)[0])
            if not target:
                continue
            checked += 1
            resolved = (document.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                failures.append(f"{document.relative_to(root)}: link escapes repository: {raw}")
                continue
            if not resolved.exists():
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{document.relative_to(root)}:{line}: missing link target: {raw}")
                continue
            if resolved not in tracked and not (
                resolved.is_dir() and any(resolved in path.parents for path in tracked)
            ):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{document.relative_to(root)}:{line}: untracked link target: {raw}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"documentation links ok ({checked} relative links checked)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
