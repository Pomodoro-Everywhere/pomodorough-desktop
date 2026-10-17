"""R49-D01 residual: uncaught exits for remaining nonproduction launch forms."""

from __future__ import annotations

import pytest

from pomodorough import __version__
from tests.test_r49_d01_sentry_launch import run_child

ALLOW = "POMODOROUGH_SENTRY_ALLOW_IN_TESTS"


@pytest.mark.parametrize(
    "markers",
    [
        pytest.param({"GITHUB_ACTIONS": "true"}, id="github-actions"),
        pytest.param({"POMODOROUGH_SHARED_CORE_SMOKE": "1"}, id="core-smoke"),
        pytest.param(
            {"POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST": "1"}, id="oauth-self-test"
        ),
        pytest.param(
            {"POMODOROUGH_PLATFORM_STORE_SELF_TEST": "1"}, id="store-self-test"
        ),
        pytest.param({"PYTEST_CURRENT_TEST": "standalone-child"}, id="pytest-child"),
    ],
)
def test_remaining_nonproduction_uncaught_exits_stay_silent(tmp_path, markers):
    receipt = run_child(tmp_path, markers, preinitialized=True, uncaught=True)
    assert receipt["initialized"] is False
    assert receipt["client_active"] is True
    assert receipt["events"] == []
    assert receipt["runners"] == []


@pytest.mark.parametrize(
    "markers",
    [
        pytest.param({"GITHUB_ACTIONS": "true", ALLOW: "1"}, id="github-opt-in"),
        pytest.param({"POMODOROUGH_SHARED_CORE_SMOKE": "1", ALLOW: "1"}, id="smoke-opt-in"),
    ],
)
def test_remaining_nonproduction_uncaught_opt_in_reports_once(tmp_path, markers):
    receipt = run_child(tmp_path, markers, preinitialized=True, uncaught=True)
    assert receipt["initialized"] is True
    assert len(receipt["events"]) == 1
    event = receipt["events"][0]
    assert event["environment"] == "production"
    assert event["release"] == __version__
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"
