#!/usr/bin/env python3
"""Fail CI when a repository-relative Markdown link points to a missing path."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
IGNORED_PREFIXES = ("http://", "https://", "mailto:", "#", "data:")
IGNORED_PARTS = {".git", "node_modules", "dist", "build", ".build", ".gradle", "DerivedData"}

def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    checked = 0
    for document in root.rglob("*.md"):
        if any(part in IGNORED_PARTS for part in document.relative_to(root).parts):
            continue
        text = document.read_text(encoding="utf-8")
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
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"documentation links ok ({checked} relative links checked)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
