from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_TAG = "v1.2.3"


def release_source_commit_gate() -> str:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split(
        "      - name: Verify manual release source commit\n", 1
    )[1].split("      - name: Validate tag and version metadata\n", 1)[0]
    script = step.split("        run: |\n", 1)[1]
    return textwrap.dedent(script)


class ReleaseSourceCommitGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name)
        self.git("init", "--quiet")
        self.git("config", "user.email", "release-contract@example.invalid")
        self.git("config", "user.name", "Release Contract")
        workflow = self.repository / ".github" / "workflows" / "release.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("tagged workflow\n", encoding="utf-8")
        test = self.repository / "tests" / "test_release_contract.py"
        test.parent.mkdir()
        test.write_text("tagged test\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "tag source")
        self.git("tag", "-a", RELEASE_TAG, "-m", "release")
        self.tag_commit = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            capture_output=True,
            check=True,
            text=True,
        )

    def run_gate(self, github_sha: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(GITHUB_SHA=github_sha, RELEASE_TAG=RELEASE_TAG)
        return subprocess.run(
            ["bash", "-c", release_source_commit_gate()],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )

    def test_exact_peeled_tag_commit_passes(self) -> None:
        result = self.run_gate(self.tag_commit)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_descendant_workflow_and_test_commit_rejects(self) -> None:
        workflow = self.repository / ".github" / "workflows" / "release.yml"
        workflow.write_text("post-tag workflow\n", encoding="utf-8")
        test = self.repository / "tests" / "test_release_contract.py"
        test.write_text("post-tag test\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "post-tag workflow and test")
        descendant_commit = self.git("rev-parse", "HEAD").stdout.strip()

        result = self.run_gate(descendant_commit)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(descendant_commit, result.stderr)
        self.assertIn(self.tag_commit, result.stderr)


if __name__ == "__main__":
    unittest.main()
