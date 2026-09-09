from pathlib import Path
import re

import pytest


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/release.yml"
DEPENDENCIES = {
    "ci": ["validate-release"],
    "package": ["validate-release"],
    "flatpak": ["validate-release"],
    "windows": ["validate-release"],
    "release": ["ci", "package", "flatpak", "windows"],
    "homebrew": ["release"],
}


def assert_release_dependencies(workflow: str) -> None:
    for name, expected in DEPENDENCIES.items():
        matches = re.findall(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|\Z)", workflow)
        assert len(matches) == 1, f"Missing or duplicate job: {name}"
        job = matches[0]
        needs = re.findall(r"(?m)^    needs:([^\n]*)(\n(?:      - [^\n]+\n)*)", job)
        assert len(needs) == 1, f"Missing or duplicate dependencies: {name}"
        inline, block = needs[0]
        actual = [inline.strip()] if inline.strip() else re.findall(r"- (\S+)", block)
        assert actual == expected, f"Wrong dependencies: {name}"
        assert not re.search(r"(?m)^    (if|continue-on-error):", job), name
    assert "    uses: ./.github/workflows/ci.yml\n" in workflow


def test_packaging_overlaps_ci_but_publication_requires_all_gates():
    assert_release_dependencies(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("gate", ["ci", "package", "flatpak", "windows"])
def test_missing_publication_gate_is_rejected(gate):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    changed = workflow.replace(f"      - {gate}\n", "", 1)
    with pytest.raises(AssertionError, match="Wrong dependencies: release"):
        assert_release_dependencies(changed)


@pytest.mark.parametrize("job", ["package", "flatpak", "windows"])
def test_serial_packaging_dependency_is_rejected(job):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index(f"  {job}:\n")
    changed = workflow[:start] + workflow[start:].replace(
        "    needs: validate-release\n", "    needs: ci\n", 1,
    )
    with pytest.raises(AssertionError, match=f"Wrong dependencies: {job}"):
        assert_release_dependencies(changed)


@pytest.mark.parametrize("job", DEPENDENCIES)
@pytest.mark.parametrize("override", ["if: always()", "continue-on-error: true"])
def test_failure_bypass_is_rejected(job, override):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    changed = workflow.replace(f"  {job}:\n", f"  {job}:\n    {override}\n", 1)
    with pytest.raises(AssertionError, match=job):
        assert_release_dependencies(changed)
