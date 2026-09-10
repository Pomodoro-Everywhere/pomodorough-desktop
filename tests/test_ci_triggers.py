from pathlib import Path

import pytest

from tests.test_macos_ci_coverage_d5 import WorkflowYaml
from tests.test_release_job_dependencies import assert_release_dependencies


WORKFLOWS = Path(__file__).parents[1] / ".github/workflows"


def assert_ci_triggers(source: str) -> None:
    triggers = WorkflowYaml(source).parse()["on"]
    assert set(triggers) == {
        "push", "pull_request", "workflow_call", "workflow_dispatch",
    }
    assert triggers["push"] == {"branches": ["**"]}
    assert triggers["pull_request"] is None
    assert triggers["workflow_call"] is None
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"candidate-request", "expected-sha", "command-sha256"}
    assert all(value["required"] is True for value in inputs.values())


def test_standalone_ci_covers_all_branches_and_release_calls_ci():
    assert_ci_triggers((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert_release_dependencies(release)
    assert WorkflowYaml(release).parse()["on"]["push"] == {"tags": ["v*"]}


@pytest.mark.parametrize("replacement", [
    "  push:",
    '  push:\n    tags:\n      - "**"',
    '  push:\n    branches:\n      - "main"',
    '  push:\n    branches:\n      - "*"',
    '  push:\n    branches:\n      - "**"\n    tags:\n      - "v*"',
])
def test_duplicate_tag_runs_and_missing_branch_coverage_are_rejected(replacement):
    source = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    start, end = source.index("  push:"), source.index("  pull_request:")
    with pytest.raises(AssertionError):
        assert_ci_triggers(source[:start] + replacement + "\n" + source[end:])


@pytest.mark.parametrize("event", ["pull_request", "workflow_call", "workflow_dispatch"])
def test_required_ci_entry_points_cannot_be_removed(event):
    source = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    changed = source.replace(f"  {event}:", f"  removed-{event}:", 1)
    with pytest.raises(AssertionError):
        assert_ci_triggers(changed)
