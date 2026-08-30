from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
import textwrap
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PYTEST_PIN = "pytest==9.1.1"
UV_PIN = "uv==0.12.5"
UV_RUN_PREFIX = ["uv", "run", "--frozen", "--with", PYTEST_PIN]
CI_TEST_COMMAND = [
    *UV_RUN_PREFIX, "python", "-m", "pytest", "tests", "-v",
    "--junitxml=pytest-results.xml",
]
FULL_RUNS = [
    ("ci.yml", "Run tests", "Install package and test runner", "python", "."),
    ("release.yml", "Install and test wheel", "Install and test wheel",
     ".release-wheel/bin/python", "dist/*.whl"),
    ("release.yml", "Install and test source distribution",
     "Install and test source distribution", ".release-sdist/bin/python", "dist/*.tar.gz"),
]


def workflow_step(workflow: str, name: str) -> str:
    return workflow.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]


def workflow_environment(scope: str, indentation: int) -> dict[str, str]:
    prefix = " " * indentation
    assert not re.search(rf"(?m)^{prefix}['\"]?<<['\"]?\s*:", scope), "Unsupported environment merge"
    headers = list(re.finditer(rf"(?m)^{prefix}['\"]?env['\"]?\s*:[^\n]*(?:\n|$)", scope))
    assert len(headers) <= 1, "Duplicate environment block"
    if not headers:
        return {}
    header = headers[0]
    assert header.group().strip() == "env:", "Unsupported environment block"
    environment = {}
    for line in scope[header.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indentation:
            break
        entry = re.fullmatch(rf"{prefix}  ([A-Z_][A-Z0-9_]*):[ \t]*(.*)", line)
        assert entry is not None, "Unsupported environment entry"
        name, value = entry.groups()
        assert name not in environment, f"Duplicate environment variable: {name}"
        environment[name] = value
    return environment


def assert_full_suite_environment(workflow: str, test_step: str, install_step: str, python: str):
    _, jobs = workflow.split("\njobs:\n", 1)
    assert workflow_environment(workflow, 0).keys() <= {
        "CORE_COMMIT", "CORE_RELEASE_TAG", "CORE_SHA256", "RELEASE_TAG",
    }, "Unreviewed workflow environment"
    job_name = "tests" if python == "python" else "package"
    matches = re.findall(rf"(?ms)^  {job_name}:\n(.*?)(?=^  \S|\Z)", jobs)
    assert len(matches) == 1, "Unsupported full-suite job"
    job = matches[0]
    assert "    steps:\n" in job, "Unsupported full-suite job"
    assert f"      - name: {test_step}\n" in job
    assert f"      - name: {install_step}\n" in job
    assert not workflow_environment(job, 4), "Unreviewed job environment"
    assert not workflow_environment(workflow_step(job, install_step), 8), "Unreviewed install environment"
    expected = {"QT_QPA_PLATFORM": "offscreen", "UV_PYTHON": "${{ matrix.python-version }}"}
    assert workflow_environment(workflow_step(job, test_step), 8) == (
        expected if python == "python" else {}
    ), "Unreviewed test environment"


def full_test_command(step: str) -> list[str]:
    commands = [line.strip().removeprefix("run: ") for line in step.splitlines()
                if " -m pytest " in line]
    assert len(commands) == 1, "Each full-suite step must invoke pytest once"
    return shlex.split(commands[0].removeprefix("QT_QPA_PLATFORM=offscreen "))


def fixture_test_command(command: list[str]) -> list[str]:
    if command == CI_TEST_COMMAND:
        command = command[len(UV_RUN_PREFIX):]
    else:
        assert command in [
            [python, "-m", "pytest", "tests", "-v"]
            for filename, _, _, python, _ in FULL_RUNS if filename == "release.yml"
        ], "Unsupported full-suite runner"
    return [sys.executable, *command[1:]]


def run_tests(command: list[str], directory: Path, home: Path, *arguments: str):
    replay = fixture_test_command(command)
    home.mkdir()
    environment = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                       XDG_CONFIG_HOME=str(home / "config"),
                       XDG_DATA_HOME=str(home / "data"),
                       QT_QPA_PLATFORM="offscreen", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [*replay, *arguments], cwd=directory, env=environment,
        capture_output=True, text=True, timeout=120, check=False,
    )


def assert_full_suite_contract(
    workflow: str, test_step: str, install_step: str, python: str, artifact: str,
):
    assert_full_suite_environment(workflow, test_step, install_step, python)
    install = workflow_step(workflow, install_step)
    suite = workflow_step(workflow, test_step)
    install_commands = [line.strip().removeprefix("run: ") for line in install.splitlines()]
    assert f"{python} -m pip install {artifact} {PYTEST_PIN}" in install_commands
    assert workflow.index(f"- name: {install_step}") <= workflow.index(f"- name: {test_step}")
    expected = [python, "-m", "pytest", "tests", "-v"]
    if python == "python":
        expected = CI_TEST_COMMAND
        assert f"python -m pip install --disable-pip-version-check --no-deps {UV_PIN}" in install_commands
    else:
        assert f"QT_QPA_PLATFORM=offscreen {shlex.join(expected)}" in suite
    assert full_test_command(suite) == expected
    if install_step == test_step:
        assert suite.index(" -m pip install ") < suite.index(" -m pytest ")
    assert "PYTHONPATH" not in workflow
    assert "unittest discover" not in workflow


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS)
def test_full_suite_installs_test_only_runner_before_execution(
    filename, test_step, install_step, python, artifact,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    assert_full_suite_contract(workflow, test_step, install_step, python, artifact)


@pytest.mark.parametrize("before,after", [
    pytest.param("uv run", "uvx run", id="unknown-wrapper"),
    pytest.param("uv run", "uv exec", id="wrong-subcommand"),
    pytest.param("--frozen ", "", id="missing-frozen"),
    pytest.param("--frozen", "--frozen --no-sync", id="extra-wrapper-option"),
    pytest.param("--with ", "", id="missing-with"),
    pytest.param("pytest==9.1.1", "pytest", id="unpinned-runner"),
    pytest.param("pytest==9.1.1", "pytest==9.0.2", id="wrong-runner-pin"),
    pytest.param("python -m", "python3 -m", id="wrong-interpreter"),
    pytest.param("pytest tests", "pytest tests/test_probe.py", id="single-module"),
    pytest.param("pytest tests", "pytest tests/test_probe.py::test_function", id="single-node"),
    pytest.param("pytest tests", "pytest", id="missing-selection"),
    pytest.param("tests -v", "tests -v -k test_function", id="keyword-filter"),
    pytest.param("tests -v", "tests -v -m smoke", id="marker-filter"),
    pytest.param("tests -v", "tests -v -x", id="fail-fast"),
    pytest.param("tests -v", "tests -v --maxfail=1", id="failure-limit"),
    pytest.param("tests -v", "tests -q", id="missing-verbose"),
    pytest.param(" --junitxml=pytest-results.xml", "", id="missing-junit"),
    pytest.param("pytest-results.xml", "other-results.xml", id="wrong-junit"),
    pytest.param("--junitxml=pytest-results.xml", "--junitxml=pytest-results.xml --junitxml=other.xml",
                 id="overridden-junit"),
])
def test_ci_runner_rejects_command_drift(before, after):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    original = shlex.join(CI_TEST_COMMAND)
    assert workflow.count(original) == 1
    assert before in original
    changed = original.replace(before, after, 1)
    with pytest.raises(AssertionError):
        assert_full_suite_contract(workflow.replace(original, changed, 1), *FULL_RUNS[0][1:])
    with pytest.raises(AssertionError, match="Unsupported full-suite runner"):
        fixture_test_command(shlex.split(changed))


@pytest.mark.parametrize("before,after", [
    ("python -m pip install . pytest==9.1.1", "python -m pip install . pytest"),
    ("python -m pip install . pytest==9.1.1", "python -m pip install . pytest==9.0.2"),
    ("python -m pip install . pytest==9.1.1", "python -m pip install ."),
    ("--no-deps uv==0.12.5", "--no-deps uv"),
    ("--no-deps uv==0.12.5", "--no-deps uv==0.12.4"),
    ("python -m pip install --disable-pip-version-check --no-deps uv==0.12.5", ""),
    ("QT_QPA_PLATFORM: offscreen", "QT_QPA_PLATFORM: xcb"),
    ("QT_QPA_PLATFORM: offscreen", ""),
    ("UV_PYTHON: ${{ matrix.python-version }}", 'UV_PYTHON: "3.11"'),
    ("UV_PYTHON: ${{ matrix.python-version }}", ""),
])
def test_ci_runner_rejects_install_or_environment_drift(before, after):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count(before) == 1
    with pytest.raises(AssertionError):
        assert_full_suite_contract(workflow.replace(before, after, 1), *FULL_RUNS[0][1:])


@pytest.mark.parametrize("run_index,anchor,entry", [
    (0, "env:\n", '  PYTEST_ADDOPTS: "-x"\n'),
    (0, "env:\n", '  PYTEST_ADDOPTS: "-k test_function"\n'),
    (0, "  tests:\n", '    env:\n      PYTEST_ADDOPTS: "-x"\n'),
    (0, "  tests:\n", '    env:\n      PYTEST_ADDOPTS: "-k test_function"\n'),
    (0, "        env:\n          QT_QPA_PLATFORM: offscreen\n", '          PYTEST_ADDOPTS: "-x"\n'),
    (0, "        env:\n          QT_QPA_PLATFORM: offscreen\n",
     '          PYTEST_ADDOPTS: "-k test_function"\n'),
    (0, "      - name: Install package and test runner\n",
     '        env:\n          PYTHONHOME: /tmp/alternate\n'),
    (0, "  tests:\n", '    env:\n      PYTEST_PLUGINS: alternate\n'),
    (0, "env:\n", '  PYTEST_DISABLE_PLUGIN_AUTOLOAD: "1"\n'),
    (0, "env:\n", '  PYTHONOPTIMIZE: "1"\n'),
    (0, "  tests:\n", '    env:\n      PYTHONWARNINGS: ignore\n'),
    (0, "  tests:\n", '    env:\n      PYTHONPATH: /tmp/alternate\n'),
    (1, "env:\n", '  PYTEST_ADDOPTS: "-x"\n'),
    (2, "  package:\n", '    env:\n      PYTEST_ADDOPTS: "-k test_function"\n'),
    (1, "      - name: Install and test wheel\n", '        env:\n          PYTEST_ADDOPTS: "-x"\n'),
    (2, "      - name: Install and test source distribution\n",
     '        env:\n          PYTEST_ADDOPTS: "-k test_function"\n'),
])
def test_full_suite_rejects_environment_overrides(run_index, anchor, entry):
    filename, *arguments = FULL_RUNS[run_index]
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    assert anchor in workflow
    with pytest.raises(AssertionError, match="environment"):
        assert_full_suite_contract(workflow.replace(anchor, anchor + entry, 1), *arguments)


@pytest.mark.parametrize("before,after", [
    ("  tests:\n", '  tests:\n    env: {PYTEST_ADDOPTS: "-x"}\n'),
    ("  tests:\n", "  tests:\n    env: ${{ matrix.environment }}\n"),
    ("  tests:\n", "  tests:\n    env: *runner_environment\n"),
    ("  tests:\n", "  tests:\n    <<: *runner_defaults\n"),
    ("env:\n", '"env":\n  PYTEST_ADDOPTS: "-x"\n'),
    ("env:\n", "env: &runner_environment\n  PYTEST_ADDOPTS: '-x'\n"),
    ("env:\n", 'env:\n  "PYTEST_ADDOPTS": "-x"\n'),
    ("env:\n", 'env:\n  pytest_addopts: "-x"\n'),
    ("env:\n", "env:\n  <<: *runner_environment\n"),
    ("env:\n", 'env:\n  PYTEST_ADDOPTS: >-\n    -k test_function\n'),
    ("          UV_PYTHON: ${{ matrix.python-version }}\n",
     '          UV_PYTHON: ${{ matrix.python-version }}\n          UV_PYTHON: "3.11"\n'),
    ("        run: uv run", '        env:\n          PYTEST_ADDOPTS: "-x"\n        run: uv run'),
])
def test_ci_runner_rejects_unreviewed_environment_shapes(before, after):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert before in workflow
    with pytest.raises(AssertionError, match="environment"):
        assert_full_suite_contract(workflow.replace(before, after, 1), *FULL_RUNS[0][1:])


@pytest.mark.parametrize("anchor,entry", [
    ("  quality:\n", '    env:\n      PYTEST_ADDOPTS: "-x"\n'),
    ("      - name: Test optional Iroh runtime\n", '        env:\n          PYTEST_ADDOPTS: "-x"\n'),
    ("        env:\n          QT_QPA_PLATFORM: offscreen\n", '          # PYTEST_ADDOPTS: "-x"\n'),
])
def test_ci_runner_environment_guard_ignores_unrelated_scopes_and_comments(anchor, entry):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert anchor in workflow
    assert_full_suite_contract(workflow.replace(anchor, anchor + entry, 1), *FULL_RUNS[0][1:])


@pytest.mark.parametrize("scope", ["workflow", "job"])
def test_ci_runner_rejects_environment_after_jobs_or_steps(scope):
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if scope == "workflow":
        start = workflow.index("\nenv:\n")
        end = workflow.index("\njobs:\n")
        environment = workflow[start:end] + '  PYTEST_ADDOPTS: "-x"\n'
        changed = workflow[:start] + workflow[end:] + environment
    else:
        changed = workflow.replace("\n  quality:\n", '\n    env:\n      PYTEST_ADDOPTS: "-x"\n\n  quality:\n')
    with pytest.raises(AssertionError, match="environment"):
        assert_full_suite_contract(changed, *FULL_RUNS[0][1:])


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS)
def test_fixture_replay_preserves_pytest_arguments(
    filename, test_step, install_step, python, artifact,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    command = full_test_command(workflow_step(workflow, test_step))
    original = command.copy()
    expected = [sys.executable, "-m", "pytest", "tests", "-v"]
    if filename == "ci.yml":
        expected.append("--junitxml=pytest-results.xml")
    assert fixture_test_command(command) == expected
    assert command == original


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS[1:])
@pytest.mark.parametrize("before,after", [
    (" -m pytest", " -m unittest"),
    ("tests -v", "tests/test_probe.py -v"),
    ("tests -v", "tests -v -k test_function"),
    ("tests -v", "tests -v --junitxml=pytest-results.xml"),
])
def test_release_runner_rejects_command_drift(
    filename, test_step, install_step, python, artifact, before, after,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    original = shlex.join([python, "-m", "pytest", "tests", "-v"])
    assert workflow.count(original) == 1
    changed = original.replace(before, after, 1)
    with pytest.raises(AssertionError):
        assert_full_suite_contract(workflow.replace(original, changed, 1),
                                   test_step, install_step, python, artifact)
    with pytest.raises(AssertionError, match="Unsupported full-suite runner"):
        fixture_test_command(shlex.split(changed))


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS)
def test_full_runners_collect_every_test_module(
    filename, test_step, install_step, python, artifact, tmp_path,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    command = full_test_command(workflow_step(workflow, test_step))
    result = run_tests(command, ROOT, tmp_path / "home", "--collect-only", "-qq")
    assert result.returncode == 0, result.stdout + result.stderr
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").glob("test_*.py")
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name.startswith("test_")
               for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
    }
    collected = set(re.findall(r"^(tests/[^:\r\n]+)::", result.stdout, re.MULTILINE))
    assert collected == expected


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS)
def test_full_runners_execute_functions_and_all_unittest_subtests(
    filename, test_step, install_step, python, artifact, tmp_path,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    command = full_test_command(workflow_step(workflow, test_step))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_probe.py").write_text(textwrap.dedent("""\
        import unittest
        from pathlib import Path
        import pytest

        @pytest.mark.parametrize("value", [0, 1, 2])
        def test_function(value):
            Path(f"function-{value}").touch()
            assert value != 1

        class SubtestProbe(unittest.TestCase):
            def test_subtests(self):
                for value in (0, 1, 2):
                    with self.subTest(value=value):
                        Path(f"subtest-{value}").touch()
                        self.assertNotEqual(value, 1)
        """), encoding="utf-8")
    result = run_tests(command, tmp_path, tmp_path / "home")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test_function[1] FAILED" in result.stdout
    assert "SubtestProbe.test_subtests (value=1)" in result.stdout
    assert all((tmp_path / f"function-{value}").exists() for value in (0, 1, 2))
    assert all((tmp_path / f"subtest-{value}").exists() for value in (0, 1, 2))
    if filename == "ci.yml":
        cases = ElementTree.parse(tmp_path / "pytest-results.xml").findall(".//testcase")
        failed = {case.attrib["name"] for case in cases if case.find("failure") is not None}
        assert "test_function[1]" in failed
        assert any("test_subtests" in name for name in failed)


def test_documented_runner_matches_ci_without_runtime_dependency():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"uv run --frozen --with {PYTEST_PIN} python -m pytest tests -v" in readme
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"] + [
        dependency for extra in project["optional-dependencies"].values() for dependency in extra
    ]
    assert not any(dependency.startswith("pytest") for dependency in dependencies)


def test_optional_iroh_runtime_step_remains_enabled():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    step = workflow_step(workflow, "Test optional Iroh runtime")
    assert "if: runner.os == 'Linux' || runner.os == 'Windows'" in step
    assert 'python -m pip install ".[iroh]"' in step
    assert "iroh.SecretKey.generate().to_bytes()" in step
    assert "QT_QPA_PLATFORM=offscreen python -m unittest tests.test_iroh_network -v" in step
