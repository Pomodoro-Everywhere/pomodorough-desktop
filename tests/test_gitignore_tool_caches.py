"""D28 proof: type-check, tox, and property-test caches stay untracked."""

from __future__ import annotations

import fnmatch
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
GITIGNORE = ROOT / ".gitignore"
REQUIRED = (".mypy_cache/", ".tox/", ".hypothesis/")


def _gitignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class GitignoreToolCachesTests(unittest.TestCase):
    def test_required_patterns_are_present(self) -> None:
        patterns = _gitignore_patterns()
        for required in REQUIRED:
            with self.subTest(required=required):
                self.assertIn(required, patterns)

    def test_required_paths_would_be_ignored(self) -> None:
        patterns = _gitignore_patterns()
        cases = (
            ".mypy_cache/foo.data",
            ".tox/py314/file",
            ".hypothesis/examples/abc",
        )
        for case in cases:
            with self.subTest(case=case):
                matched = any(
                    fnmatch.fnmatch(case, pattern)
                    or fnmatch.fnmatch(Path(case).name, pattern)
                    or case.startswith(pattern.rstrip("/"))
                    or case == pattern
                    for pattern in patterns
                )
                self.assertTrue(matched, f"{case} is not ignored")

    def test_no_cache_paths_are_tracked(self) -> None:
        completed = subprocess.run(
            ["git", "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        tracked = completed.stdout.splitlines()
        for entry in tracked:
            with self.subTest(entry=entry):
                self.assertFalse(
                    entry.startswith(".mypy_cache/")
                    or entry.startswith(".tox/")
                    or entry.startswith(".hypothesis/")
                    or entry == ".mypy_cache"
                    or entry == ".tox"
                    or entry == ".hypothesis",
                    f"{entry} should not be tracked",
                )


if __name__ == "__main__":
    unittest.main()
