"""R49-D01 launch eligibility through a real SDK and fresh subprocesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pomodorough import __version__
from tests.test_ci_test_runner import workflow_environment, workflow_step

ROOT = Path(__file__).parents[1]
CHILD = Path(__file__).with_name("r49_d01_sentry_child.py")
DISABLE = "POMODOROUGH_SENTRY_DISABLE"
ALLOW = "POMODOROUGH_SENTRY_ALLOW_IN_TESTS"
LAUNCHES = [
    pytest.param({"CI": "true"}, None, id="ci"),
    pytest.param({"GITHUB_ACTIONS": "true"}, None, id="github-actions"),
    pytest.param({"POMODOROUGH_SCREENSHOT": "smoke.png"}, None, id="screenshot"),
    pytest.param({"POMODOROUGH_SHARED_CORE_SMOKE": "1"}, None, id="core-smoke"),
    pytest.param({"POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST": "1"}, None, id="oauth-self-test"),
    pytest.param({"POMODOROUGH_PLATFORM_STORE_SELF_TEST": "1"}, None, id="store-self-test"),
    pytest.param({"PYTEST_CURRENT_TEST": "standalone-child"}, None, id="pytest-child"),
    pytest.param({}, "pytest", id="pytest-imported"),
    pytest.param({}, "unittest", id="unittest-imported"),
]


def run_child(tmp_path, markers, **options):
    packaged_default = tmp_path / "sentry_dsn_default"
    packaged_default.write_text("https://public@example.invalid/1", encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"CI", "GITHUB_ACTIONS", "SENTRY_DSN"}
        and not key.startswith(("POMODOROUGH_", "PYTEST_", "SENTRY_"))
    }
    environment.update(
        HOME=str(tmp_path), USERPROFILE=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path),
        QT_QPA_PLATFORM="offscreen",
        POMODOROUGH_SENTRY_PACKAGED_DEFAULT_FILE=str(packaged_default),
    )
    environment.update(markers)
    result = subprocess.run(
        [sys.executable, str(CHILD), json.dumps(options)], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (1 if options.get("uncaught") else 0), result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert len(receipt["failures"]) == (0 if options.get("uncaught") else 1)
    assert receipt["python_calls"] == 1
    assert len(receipt["qt_calls"]) == (0 if options.get("uncaught") else 2)
    assert receipt["fallback_count"] == 0
    assert receipt["network_attempts"] == []
    assert result.stderr.count("Traceback (most recent call last):") == 1
    assert result.stderr.count("RuntimeError: R49-D01 ") == 1
    return receipt


@pytest.mark.parametrize("markers,runner", LAUNCHES)
def test_standalone_nonproduction_launch_is_silent(tmp_path, markers, runner):
    receipt = run_child(tmp_path, markers, runner=runner)
    assert receipt["initialized"] is False
    assert receipt["events"] == []
    if runner is None:
        assert receipt["runners"] == []


@pytest.mark.parametrize("markers,runner", LAUNCHES)
def test_capture_and_hooks_stay_silent_with_external_client(tmp_path, markers, runner):
    receipt = run_child(tmp_path, markers, runner=runner, preinitialized=True)
    assert receipt["initialized"] is False
    assert receipt["events"] == []
    assert receipt["client_active"] is True


@pytest.mark.parametrize("markers,runner", LAUNCHES)
def test_explicit_test_opt_in_preserves_reporting(tmp_path, markers, runner):
    receipt = run_child(tmp_path, {**markers, ALLOW: " YES "}, runner=runner)
    assert_reporting(receipt)


def assert_reporting(receipt):
    assert receipt["initialized"] is True
    events = receipt["events"]
    assert len(events) == 4
    assert all(event["environment"] == "production" for event in events)
    assert all(event["release"] == __version__ for event in events)
    assert [event["exception"]["values"][-1]["type"] for event in events[:2]] == [
        "PermissionError", "RuntimeError",
    ]
    assert [event["message"] for event in events[2:]] == [
        "R49-D01 synthetic Qt critical hook", "R49-D01 synthetic Qt fatal hook",
    ]


def test_ordinary_packaged_launch_reports_infrastructure_and_hooks(tmp_path):
    receipt = run_child(tmp_path, {})
    assert receipt["runners"] == []
    assert_reporting(receipt)


def test_inherited_default_sdk_reports_production_exactly_once(tmp_path):
    assert_reporting(run_child(tmp_path, {}, preinitialized=True))


def test_inherited_hook_suppression_leaves_external_sdk_active(tmp_path):
    receipt = run_child(tmp_path, {"CI": "true"}, preinitialized=True, external_control=True)
    assert receipt["initialized"] is False and receipt["client_active"] is True
    assert [event["message"] for event in receipt["events"]] == [
        "R49-D01 external SDK remains active",
    ]


@pytest.mark.parametrize("markers,expected_init,expected_count", [
    pytest.param({"CI": "true"}, False, 0, id="ci"),
    pytest.param({"POMODOROUGH_SCREENSHOT": "smoke.png"}, False, 0, id="screenshot"),
    pytest.param({"CI": "true", ALLOW: "1"}, True, 1, id="ci-opt-in"),
    pytest.param({}, True, 1, id="production"),
])
def test_inherited_default_sdk_actual_uncaught_exit(
    tmp_path, markers, expected_init, expected_count,
):
    receipt = run_child(tmp_path, markers, preinitialized=True, uncaught=True)
    assert receipt["initialized"] is expected_init
    assert receipt["client_active"] is True
    assert len(receipt["events"]) == expected_count
    if expected_count:
        event = receipt["events"][0]
        assert event["environment"] == "production" and event["release"] == __version__
        assert event["exception"]["values"][-1]["type"] == "RuntimeError"


def test_false_markers_do_not_disable_production(tmp_path):
    receipt = run_child(tmp_path, {
        "CI": "false", "GITHUB_ACTIONS": "0", "POMODOROUGH_SCREENSHOT": "",
        "PYTEST_CURRENT_TEST": "", "POMODOROUGH_SHARED_CORE_SMOKE": "no",
        "POMODOROUGH_OAUTH_ARTIFACT_SELF_TEST": "off",
        "POMODOROUGH_PLATFORM_STORE_SELF_TEST": "false",
    })
    assert_reporting(receipt)


def test_disable_beats_opt_in_and_force_at_startup(tmp_path):
    receipt = run_child(tmp_path, {DISABLE: "1", ALLOW: "1"}, force=True)
    assert receipt["initialized"] is False
    assert receipt["events"] == []


def test_explicit_environment_detects_ci(tmp_path):
    receipt = run_child(tmp_path, {}, environment={"CI": "true"})
    assert receipt["initialized"] is False
    assert receipt["events"] == []


def test_empty_environment_cannot_hide_process_ci(tmp_path):
    receipt = run_child(tmp_path, {"GITHUB_ACTIONS": "true"}, environment={})
    assert receipt["initialized"] is False
    assert receipt["events"] == []


def test_false_test_opt_in_keeps_ci_silent(tmp_path):
    receipt = run_child(tmp_path, {"CI": "true", ALLOW: "false"})
    assert receipt["initialized"] is False
    assert receipt["events"] == []


@pytest.mark.parametrize("filename,name", [
    ("release.yml", "Install and test wheel"),
    ("release.yml", "Install and test source distribution"),
    ("build-windows.yml", "Smoke test executable"),
])
def test_complete_smoke_step_disables_telemetry(filename, name):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    step = workflow_step(workflow, name)
    assert workflow_environment(step, 8)[DISABLE] == '"1"'
    assert "continue-on-error" not in step and "        if:" not in step
    assert "Start-Process" in step if filename == "build-windows.yml" else "timeout 30s" in step
