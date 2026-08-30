from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
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

CORE_COMMIT = "440f5364f036d02d46abca048f09b893b0134791"
CORE_RELEASE_TAG = "v0.7.0"
CORE_SHA256 = "4a58bd2b702e0d43d6f2262d21f7e940dfdf05cc911d6519b67c1b5e988d8b0b"
PROVENANCE_SCRIPT = ROOT / "scripts" / "verify_shared_core_provenance.py"
UNPACK_RELEASE_SCRIPT = ROOT / "scripts" / "unpack_release_artifacts.sh"
VERIFY_RELEASE_SCRIPT = ROOT / "scripts" / "verify_release_artifacts.sh"
VALID_WASM = b"\0asm\x01\0\0\0"
DIFFERENT_VALID_WASM = VALID_WASM + b"\0\x01\0"


class SharedCoreProvenanceTests(unittest.TestCase):
    def run_provenance(
        self,
        rebuilt_bytes: bytes,
        embedded_bytes: list[bytes],
        expected_sha256: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rebuilt = root / "rebuilt.wasm"
            rebuilt.write_bytes(rebuilt_bytes)
            embedded = []
            for index, candidate_bytes in enumerate(embedded_bytes):
                candidate = root / f"embedded-{index}.wasm"
                candidate.write_bytes(candidate_bytes)
                embedded.append(candidate)

            return subprocess.run(
                [
                    sys.executable,
                    str(PROVENANCE_SCRIPT),
                    "--sha256",
                    expected_sha256 or hashlib.sha256(rebuilt_bytes).hexdigest(),
                    str(rebuilt),
                    *(str(candidate) for candidate in embedded),
                ],
                capture_output=True,
                check=False,
                text=True,
            )

    def test_rejects_rebuilt_module_that_differs_from_pinned_embedded_modules(
        self,
    ) -> None:
        result = self.run_provenance(
            DIFFERENT_VALID_WASM,
            [VALID_WASM, VALID_WASM],
            expected_sha256=hashlib.sha256(VALID_WASM).hexdigest(),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rebuilt shared core SHA-256 is", result.stderr)

    def test_accepts_exact_rebuild_with_pinned_embedded_modules(self) -> None:
        result = self.run_provenance(VALID_WASM, [VALID_WASM, VALID_WASM])

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_embedded_hash_mismatch(self) -> None:
        result = self.run_provenance(
            VALID_WASM,
            [DIFFERENT_VALID_WASM],
            expected_sha256=hashlib.sha256(VALID_WASM).hexdigest(),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("embedded shared core SHA-256 is", result.stderr)

    def test_rejects_different_valid_embedded_module(self) -> None:
        result = self.run_provenance(
            VALID_WASM,
            [VALID_WASM, DIFFERENT_VALID_WASM],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("embedded shared core SHA-256 is", result.stderr)


class ReleaseWorkflowTests(unittest.TestCase):
    def test_ci_checks_portable_rebuild_and_exact_public_shared_core(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        quality_job = workflow.split("  quality:\n", 1)[1]
        rebuild_step = workflow.split(
            "      - name: Verify pinned source and released shared core\n", 1
        )[1].split("      - name:", 1)[0]

        self.assertIn("runs-on: macos-15", quality_job.split("  dependency-review:", 1)[0])
        self.assertIn(f'CORE_COMMIT: "{CORE_COMMIT}"', workflow)
        self.assertIn(f'CORE_RELEASE_TAG: "{CORE_RELEASE_TAG}"', workflow)
        self.assertIn(f'CORE_SHA256: "{CORE_SHA256}"', workflow)
        self.assertIn("repository: Pomodoro-Everywhere/pomodorough-core", workflow)
        self.assertIn("ref: ${{ env.CORE_COMMIT }}", workflow)
        self.assertIn(
            "cargo +1.97.1 test --all-targets --locked",
            workflow,
        )
        self.assertIn(
            "cargo +1.97.1 build --release --target wasm32-unknown-unknown --locked",
            workflow,
        )
        self.assertIn("verify_wasm_artifact.py", rebuild_step)
        self.assertIn('--sha256 "$CORE_SHA256"', rebuild_step)
        self.assertIn(
            "rebuilt=pomodorough-core-source/target/wasm32-unknown-unknown/"
            "release/pomodorough_core.wasm",
            rebuild_step,
        )
        self.assertIn(
            "releases/download/$CORE_RELEASE_TAG/pomodorough_core.wasm",
            rebuild_step,
        )
        self.assertIn(
            'cmp "$released" src/pomodorough/resources/pomodorough_core.wasm',
            rebuild_step,
        )
        self.assertLess(
            rebuild_step.index('verify_wasm_artifact.py \\\n            "$rebuilt"'),
            rebuild_step.index('verify_wasm_artifact.py \\\n            "$released"'),
        )
        self.assertLess(
            rebuild_step.index('cmp "$released"'),
            rebuild_step.index("grep -Fx"),
        )

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
        self.assertIn(
            "--hidden-import pomodorough.oauth_production_signoff", workflow
        )
        self.assertIn("--collect-all wasmtime", workflow)
        self.assertIn("WaitForExit(120000)", workflow)

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

    def test_flatpak_bundles_qtnetwork_kerberos_runtime(self) -> None:
        workflow = FLATPAK_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("Add QtNetwork Kerberos runtime dependency", workflow)
        self.assertIn("- name: krb5", workflow)
        self.assertIn(
            "https://kerberos.org/dist/krb5/1.22/krb5-1.22.1.tar.gz",
            workflow,
        )
        self.assertIn(
            "sha256: 1a8832b8cad923ebbf1394f67e2efcf41e3a49f460285a66e35adec8fa0053af",
            workflow,
        )

    def test_final_platform_artifacts_validate_exact_oauth_resource(self) -> None:
        windows = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        flatpak = FLATPAK_WORKFLOW.read_text(encoding="utf-8")

        for workflow in (windows, flatpak):
            self.assertIn("POMODOROUGH_EXPECTED_OAUTH_CLIENT_ID", workflow)
        self.assertIn("oauth-client.json", launcher)
        self.assertIn("client_secret", launcher)
        self.assertIn("oauth-client.json", flatpak)
        self.assertIn("client_secret", flatpak)
        self.assertNotIn("assert config.get", flatpak)
        self.assertIn("invalid packaged OAuth resource", flatpak)

    def test_final_platform_artifacts_execute_oauth_transaction_self_test(self) -> None:
        release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        windows = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        flatpak = FLATPAK_WORKFLOW.read_text(encoding="utf-8")

        verifier_command = "-m pomodorough.oauth_artifact_verifier --self-test"
        self.assertGreaterEqual(release.count(verifier_command), 2)
        self.assertIn(verifier_command, flatpak)
        self.assertIn("POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST", windows)
        self.assertIn("oauth_artifact_verifier import main", launcher)
        self.assertIn('main(["--self-test"])', launcher)

    def test_platform_artifacts_execute_real_secure_store_roundtrip(self) -> None:
        windows = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        flatpak = FLATPAK_WORKFLOW.read_text(encoding="utf-8")
        manifest = FLATPAK_MANIFEST.read_text(encoding="utf-8")

        self.assertIn("--talk-name=org.freedesktop.secrets", manifest)
        self.assertIn("libsecret-0.21.7.tar.xz", manifest)
        self.assertIn(
            "6b452e4750590a2b5617adc40026f28d2f4903de15f1250e1d1c40bfd68ed55e",
            manifest,
        )
        self.assertIn("--platform-store-self-test", flatpak)
        self.assertIn("gnome-keyring", flatpak)
        self.assertIn("dbus-run-session", flatpak)
        self.assertIn("POMODOROUGH_PLATFORM_STORE_SELF_TEST", windows)
        self.assertIn("POMODOROUGH_PLATFORM_STORE_SELF_TEST", launcher)
        self.assertIn('main(["--platform-store-self-test"])', launcher)
        self.assertIn("--platform-store-verifier-child", launcher)
        self.assertIn('main(["--platform-store-child", *sys.argv[2:]])', launcher)

    def test_windows_artifact_exposes_production_oauth_signoff(self) -> None:
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("POMODOROUGH_OAUTH_PRODUCTION_SIGNOFF", launcher)
        self.assertIn("POMODOROUGH_OAUTH_SIGNOFF_RECEIPT", launcher)
        self.assertIn("POMODOROUGH_OAUTH_ASSET_SHA256", launcher)
        self.assertIn("--oauth-production-restart-child", launcher)
        self.assertIn("oauth_production_signoff import main", launcher)
        self.assertIn('"--artifact",\n                "windows"', launcher)

    def test_packaged_smoke_processes_never_receive_scan_secret(self) -> None:
        windows = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
        launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")
        flatpak = FLATPAK_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("COMPROMISED_GOOGLE_CLIENT_SECRET", windows)
        self.assertNotIn("COMPROMISED_GOOGLE_CLIENT_SECRET", launcher)
        self.assertNotIn("COMPROMISED_GOOGLE_CLIENT_SECRET", flatpak)

    def test_compromised_secret_scan_gates_release_publication(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publication = workflow.split("  release:", 1)[1]
        scan_index = publication.index("scripts/verify_release_artifacts.sh")
        publish_index = publication.index("--draft=false")

        self.assertLess(scan_index, publish_index)
        self.assertIn("COMPROMISED_GOOGLE_CLIENT_SECRET", publication)
        self.assertGreaterEqual(
            publication.count("scripts/verify_release_artifacts.sh"), 2
        )
        self.assertIn("flatpak ostree", publication)
        self.assertIn("uv==0.12.5", publication)

    def test_published_recovery_assets_are_unpacked_and_secret_scanned(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publication = workflow.split("      - name: Publish GitHub release", 1)[1]
        verification = publication.split("          verify_release_assets() {", 1)[1].split(
            "          }", 1
        )[0]

        self.assertIn("COMPROMISED_GOOGLE_CLIENT_SECRET", publication)
        self.assertIn("scripts/verify_release_artifacts.sh", verification)
        self.assertIn('"$published_dir"', verification)

    def test_release_verifier_checks_packaged_oauth_and_raw_and_unpacked_secrets(self) -> None:
        verifier = VERIFY_RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("unpack_release_artifacts.sh", verifier)
        self.assertIn("verify_packaged_oauth.py", verifier)
        self.assertEqual(verifier.count("scan_secret.py"), 2)
        for package_root in ("wheel", "sdist", "flatpak-root", "windows"):
            self.assertIn(f'"$scan_root/{package_root}"', verifier)

    def test_release_unpacker_covers_every_compressed_package_format(self) -> None:
        unpacker = UNPACK_RELEASE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("python -m zipfile -e", unpacker)
        self.assertIn("python -m tarfile -e", unpacker)
        self.assertIn("flatpak build-import-bundle", unpacker)
        self.assertIn("ostree", unpacker)
        self.assertIn("--user-mode", unpacker)
        self.assertIn("pyinstxtractor-ng==2026.7.3", unpacker)
        self.assertIn("Pomodorough-${version}-x86_64.flatpak", unpacker)
        self.assertIn("Pomodorough-${version}-windows-x86_64.exe", unpacker)

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
        self.assertIn(
            "wasmtime.stage { venv.pip_install Pathname.pwd/wasmtime.downloader.basename",
            homebrew,
        )
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
