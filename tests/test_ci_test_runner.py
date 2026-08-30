from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PYTEST_PIN = "pytest==9.1.1"
FULL_RUNS = [
    ("ci.yml", "Run tests", "Install package and test runner", "python", "."),
    ("release.yml", "Install and test wheel", "Install and test wheel",
     ".release-wheel/bin/python", "dist/*.whl"),
    ("release.yml", "Install and test source distribution",
     "Install and test source distribution", ".release-sdist/bin/python", "dist/*.tar.gz"),
]


def workflow_step(workflow: str, name: str) -> str:
    return workflow.split(f"      - name: {name}\n", 1)[1].split("      - name:", 1)[0]


def full_test_command(step: str) -> list[str]:
    commands = [line.strip().removeprefix("run: ") for line in step.splitlines()
                if " -m pytest " in line]
    assert len(commands) == 1, "Each full-suite step must invoke pytest once"
    return shlex.split(commands[0].removeprefix("QT_QPA_PLATFORM=offscreen "))


def run_tests(command: list[str], directory: Path, home: Path, *arguments: str):
    home.mkdir()
    environment = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                       XDG_CONFIG_HOME=str(home / "config"),
                       XDG_DATA_HOME=str(home / "data"),
                       QT_QPA_PLATFORM="offscreen", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, *command[1:], *arguments], cwd=directory, env=environment,
        capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.mark.parametrize("filename,test_step,install_step,python,artifact", FULL_RUNS)
def test_full_suite_installs_test_only_runner_before_execution(
    filename, test_step, install_step, python, artifact,
):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    install = workflow_step(workflow, install_step)
    suite = workflow_step(workflow, test_step)
    assert f"{python} -m pip install {artifact} {PYTEST_PIN}" in install
    assert workflow.index(f"- name: {install_step}") <= workflow.index(f"- name: {test_step}")
    if install_step == test_step:
        assert suite.index(" -m pip install ") < suite.index(" -m pytest ")
    assert full_test_command(suite) == [python, "-m", "pytest", "tests", "-v"]
    assert "QT_QPA_PLATFORM" in suite
    assert "PYTHONPATH" not in workflow
    assert "unittest discover" not in workflow


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

        @pytest.mark.parametrize("value", [0, 1])
        def test_function(value):
            Path(f"function-{value}").touch()
            assert value == 0

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
    assert all((tmp_path / f"function-{value}").exists() for value in (0, 1))
    assert all((tmp_path / f"subtest-{value}").exists() for value in (0, 1, 2))


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
