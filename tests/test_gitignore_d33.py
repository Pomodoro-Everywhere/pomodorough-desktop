"""D33 proof: generated screenshots and editor backups stay untracked."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
GITIGNORE = ROOT / ".gitignore"


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_ignored(path: str) -> bool:
    completed = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT,
    )
    return completed.returncode == 0


class GitignoreD33Tests(unittest.TestCase):
    def test_required_patterns_are_present(self) -> None:
        patterns = _patterns()
        for required in (
            "screenshots/*.png",
            "!screenshots/screenshot.png",
            "*~",
            "*.swp",
            "*.orig",
        ):
            with self.subTest(required=required):
                self.assertIn(required, patterns)

    def test_generated_screenshots_are_ignored(self) -> None:
        self.assertTrue(_is_ignored("screenshots/other.png"))
        self.assertTrue(_is_ignored("screenshots/run-123.png"))

    def test_reference_screenshot_is_kept(self) -> None:
        self.assertFalse(_is_ignored("screenshots/screenshot.png"))
        completed = subprocess.run(
            ["git", "ls-files", "screenshots/screenshot.png"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertIn("screenshots/screenshot.png", completed.stdout.splitlines())

    def test_editor_backups_are_ignored(self) -> None:
        for case in ("foo~", "src/bar~", "foo.swp", "foo.orig"):
            with self.subTest(case=case):
                self.assertTrue(_is_ignored(case), f"{case} is not ignored")

    def test_no_backup_paths_are_tracked(self) -> None:
        completed = subprocess.run(
            ["git", "ls-files"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        for entry in completed.stdout.splitlines():
            with self.subTest(entry=entry):
                self.assertFalse(
                    entry.endswith("~")
                    or entry.endswith(".swp")
                    or entry.endswith(".orig"),
                    f"{entry} should not be tracked",
                )
                if entry.startswith("screenshots/") and entry.endswith(".png"):
                    self.assertEqual(entry, "screenshots/screenshot.png")


if __name__ == "__main__":
    unittest.main()
