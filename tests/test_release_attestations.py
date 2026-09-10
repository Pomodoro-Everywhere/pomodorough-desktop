from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from test_release_source_commit_gate import release_gate_shell


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/release.yml"
ASSETS = (
    "Pomodorough-1.2.3-windows-x86_64.exe",
    "Pomodorough-1.2.3-x86_64.flatpak",
    "pomodorough_linux-1.2.3-py3-none-any.whl",
    "pomodorough_linux-1.2.3.tar.gz",
    "pomodorough-desktop.spdx.json",
    "SHA256SUMS.txt",
)
FAKE_GH = r"""
gh() {
  [[ "$#" == 5 && "$1" == attestation && "$2" == verify ]]
  [[ "$4" == --repo && "$5" == example/desktop ]]
  touch "$3.started"
  # Barrier proves concurrent execution without asserting elapsed wall time.
  for ((attempt = 0; attempt < 100; attempt++)); do
    started=(*.started)
    if [[ "${#started[@]}" == 6 ]]; then
      touch "$3.finished"
      [[ "$3" != "$FAILED_ASSET" ]]
      return
    fi
    sleep 0.05
  done
  return 99
}
"""


@pytest.mark.parametrize("failed_asset", ("", *ASSETS))
def test_attestations_overlap_and_fail_closed(tmp_path: Path, failed_asset: str) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = workflow.split('          expected_assets=(\n', 1)[1]
    script = '          expected_assets=(\n' + script.split(
        "          verify_release_assets() {", 1
    )[0]
    environment = dict(os.environ, FAILED_ASSET=failed_asset)
    result = subprocess.run(
        [release_gate_shell(os.name, shutil.which("git")), "-c",
         "set -euo pipefail\nversion=1.2.3\nGITHUB_REPOSITORY=example/desktop\n"
         + FAKE_GH + textwrap.dedent(script)
         + '\nverify_attestations\ntouch publication-allowed\n'],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == (1 if failed_asset else 0), result.stderr
    assert sorted(path.stem for path in tmp_path.glob("*.started")) == sorted(ASSETS)
    assert sorted(path.stem for path in tmp_path.glob("*.finished")) == sorted(ASSETS)
    assert (tmp_path / "publication-allowed").exists() == (not failed_asset)


def test_attestations_remain_required_before_scan_and_publication() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    verify = workflow.split("          verify_release_assets() {\n", 1)[1]
    verify = verify.split('          release_state="missing"', 1)[0]
    assert "              verify_attestations\n            )\n" in verify
    assert verify.index("verify_attestations") < verify.index("scripts/verify_release_artifacts.sh")
    assert '          verify_release_assets\n          gh release edit "$RELEASE_TAG"' in workflow
