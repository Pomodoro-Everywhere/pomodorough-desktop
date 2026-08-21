#!/usr/bin/env python3
"""Reject mutable third-party GitHub Action references."""
from __future__ import annotations

import re
from pathlib import Path

PIN = re.compile(r"^[0-9a-f]{40}$")
USE = re.compile(r"^\s*uses:\s*([^\s@]+)@([^\s#]+)", re.MULTILINE)
root = Path(__file__).resolve().parents[1]
failures = []
for workflow in sorted((root / ".github/workflows").glob("*.y*ml")):
    for action, ref in USE.findall(workflow.read_text(encoding="utf-8")):
        if not action.startswith("./") and not PIN.fullmatch(ref):
            failures.append(f"{workflow.relative_to(root)}: {action}@{ref}")
if failures:
    raise SystemExit("mutable GitHub Action references:\n" + "\n".join(failures))
print("GitHub Action references are commit-pinned")
