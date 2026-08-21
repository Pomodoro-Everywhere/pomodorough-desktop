from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_draft_is_verified_exactly_before_publication(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publish_sequence = workflow.split(
            '          gh release upload "$RELEASE_TAG" dist/* \\\n', 1
        )[1]

        verify_index = publish_sequence.index("          verify_release_assets\n")
        publish_index = publish_sequence.index('          gh release edit "$RELEASE_TAG"')
        self.assertLess(verify_index, publish_index)
        self.assertIn("            --draft=false", publish_sequence[publish_index:])

    def test_release_verification_covers_exact_assets_checksums_and_attestations(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        verification = workflow.split("          verify_release_assets() {", 1)[1].split(
            "          }", 1
        )[0]

        self.assertIn(
            'cmp "$RUNNER_TEMP/expected-assets.txt" "$RUNNER_TEMP/actual-assets.txt"',
            verification,
        )
        self.assertIn("sha256sum --check SHA256SUMS.txt", verification)
        self.assertIn(
            "cmp expected-manifest-assets.txt actual-manifest-assets.txt", verification
        )
        self.assertIn('for asset in "${expected_assets[@]}"; do', verification)
        self.assertIn(
            'gh attestation verify "$asset" --repo "$GITHUB_REPOSITORY"',
            verification,
        )
        self.assertIn(
            "gh attestation verify SHA256SUMS.txt --repo \"$GITHUB_REPOSITORY\"",
            verification,
        )


if __name__ == "__main__":
    unittest.main()
