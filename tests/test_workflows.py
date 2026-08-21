from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"
FLATPAK_WORKFLOW = ROOT / ".github" / "workflows" / "build-flatpak.yml"
FLATPAK_MANIFEST = ROOT / "deploy" / "flatpak" / "me.egigoka.Pomodorough.yml"
WINDOWS_LAUNCHER = ROOT / "deploy" / "windows" / "launcher.py"
HOMEBREW_FORMULA = ROOT / "deploy" / "homebrew" / "pomodorough.rb.in"
FLAKE = ROOT / "flake.nix"

CORE_COMMIT = "9a01dc8da0f1612e7a301c19cf42f3b522e61684"
CORE_SHA256 = "89fb6300324042b61d62070242cccad10e30f125885bb1b7a05af67b077bac83"


class ReleaseWorkflowTests(unittest.TestCase):
    def test_ci_rebuilds_and_verifies_pinned_shared_core(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(f'CORE_COMMIT: "{CORE_COMMIT}"', workflow)
        self.assertIn(f'CORE_SHA256: "{CORE_SHA256}"', workflow)
        self.assertIn("repository: Pomodoro-Everywhere/pomodorough-core", workflow)
        self.assertIn("ref: ${{ env.CORE_COMMIT }}", workflow)
        self.assertIn(
            "cargo +1.97.1 build --release --target wasm32-unknown-unknown --locked",
            workflow,
        )
        self.assertIn("verify_wasm_artifact.py", workflow)
        self.assertIn('--sha256 "$CORE_SHA256"', workflow)

    def test_release_checks_shared_core_in_built_distributions(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertGreaterEqual(
            workflow.count(
                'from pomodorough.shared_core import SharedCore; '
                'assert SharedCore().dispatch("core.version", {})["schemaVersion"] == 1'
            ),
            2,
        )
        self.assertIn("pomodorough/resources/pomodorough_core.wasm", workflow)
        self.assertIn('/pname = "pomodorough-linux";/ { found = 1; next }', workflow)

    def test_windows_bundle_collects_shared_core_and_wasmtime(self) -> None:
        workflow = WINDOWS_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("--hidden-import pomodorough.shared_core", workflow)
        self.assertIn("--collect-all wasmtime", workflow)

    def test_final_platform_artifacts_execute_shared_core(self) -> None:
        windows = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        flatpak = FLATPAK_WORKFLOW.read_text(encoding="utf-8")
        flake = FLAKE.read_text(encoding="utf-8")

        self.assertIn("POMODOROUGH_SHARED_CORE_SMOKE", windows)
        self.assertIn("SharedCore().dispatch", launcher)
        self.assertIn("Verify shared core in Flatpak bundle", flatpak)
        self.assertIn("flatpak run --user --command=python3", flatpak)
        self.assertIn("doInstallCheck = true;", flake)
        self.assertIn('SharedCore().dispatch("core.version", {})', flake)

    def test_platform_packages_include_wasmtime_runtime(self) -> None:
        flatpak = FLATPAK_MANIFEST.read_text(encoding="utf-8")
        homebrew = HOMEBREW_FORMULA.read_text(encoding="utf-8")
        flake = FLAKE.read_text(encoding="utf-8")

        self.assertIn(
            "wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl", flatpak
        )
        self.assertIn(
            "wasmtime-48.0.0-py3-none-manylinux2014_aarch64.whl", flatpak
        )
        self.assertIn('resource "wasmtime" do', homebrew)
        self.assertIn('venv.pip_install resource("wasmtime")', homebrew)
        self.assertIn('version = "48.0.0";', flake)
        self.assertIn("wasmtime-48.0.0-py3-none-manylinux1_x86_64.whl", flake)
        self.assertIn("wasmtime-48.0.0-py3-none-manylinux2014_aarch64.whl", flake)

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

    def test_installed_python_artifacts_run_gui_smoke_without_tracebacks(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        self.assertGreaterEqual(workflow.count("POMODOROUGH_SCREENSHOT="), 2)
        self.assertGreaterEqual(workflow.count("grep -q 'Traceback (most recent call last)'"), 2)
        self.assertIn("test -s .release-wheel-smoke.png", workflow)
        self.assertIn("test -s .release-sdist-smoke.png", workflow)


if __name__ == "__main__":
    unittest.main()
