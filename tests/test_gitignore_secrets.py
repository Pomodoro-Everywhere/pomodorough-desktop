"""D26 proof: local secrets, logs, and editor state stay untracked."""

from __future__ import annotations

import fnmatch
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
GITIGNORE = ROOT / ".gitignore"
REQUIRED = (".env", "*.log", ".idea/", ".vscode/")


def _gitignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class GitignoreSecretsTests(unittest.TestCase):
    def test_required_patterns_are_present(self) -> None:
        patterns = _gitignore_patterns()
        for required in REQUIRED:
            with self.subTest(required=required):
                self.assertIn(required, patterns)

    def test_required_paths_would_be_ignored(self) -> None:
        patterns = _gitignore_patterns()
        cases = (".env", "debug.log", ".idea/workspace.xml", ".vscode/settings.json")
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


if __name__ == "__main__":
    unittest.main()
