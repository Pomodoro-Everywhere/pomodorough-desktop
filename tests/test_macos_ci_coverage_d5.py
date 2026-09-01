from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
FULL_COMMAND = "uv run --frozen --with pytest==9.1.1 python -m pytest tests -v --junitxml=pytest-results.xml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_ACTION = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
ENTRYPOINT_ASSERTION = (
    "from importlib.metadata import distribution; from inspect import signature; "
    'expected = {"pomodorough": "pomodorough.app:main", '
    '"pomodorough-cli": "pomodorough.cli:main", '
    '"pomodorough-tui": "pomodorough.tui:main"}; '
    'entries = {entry.name: entry for entry in distribution("pomodorough-linux")'
    '.entry_points if entry.group == "console_scripts"}; '
    "assert set(entries) == set(expected); "
    "assert all(entries[name].value == target "
    "for name, target in expected.items()); "
    "loaded = {name: entries[name].load() for name in expected}; "
    "assert all(callable(target) and signature(target).bind() is not None "
    "for target in loaded.values())"
)
APP_ENTRYPOINT_SMOKE = (
    "import os; from contextlib import ExitStack; "
    "from importlib.metadata import distribution; from unittest.mock import patch; "
    'entry = next(entry for entry in distribution("pomodorough-linux").entry_points '
    'if entry.group == "console_scripts" and entry.name == "pomodorough"); '
    "target = entry.load(); stack = ExitStack(); "
    'application = stack.enter_context(patch("pomodorough.app.QApplication")); '
    'stack.enter_context(patch("pomodorough.app.QIcon")); '
    'lock = stack.enter_context(patch("pomodorough.app._instance_lock")); '
    'stack.enter_context(patch("pomodorough.app.Store")); '
    'stack.enter_context(patch("pomodorough.app.CloudService")); '
    'stack.enter_context(patch("pomodorough.app._iroh_service")); '
    'stack.enter_context(patch("pomodorough.app.MainWindow")); '
    'stack.enter_context(patch("pomodorough.app.QTimer")); '
    'stack.enter_context(patch("pomodorough.app.signal.signal")); '
    'os.environ.pop("POMODOROUGH_SCREENSHOT", None); '
    "lock.return_value.tryLock.return_value = True; "
    "application.return_value.exec.return_value = 0; assert target() == 0; "
    "application.return_value.exec.assert_called_once_with(); stack.close()"
)
ENTRYPOINT_COMMANDS = [
    ["python", "-c", ENTRYPOINT_ASSERTION],
    ["python", "-c", APP_ENTRYPOINT_SMOKE],
    ["pomodorough-cli", "--help"],
    ["pomodorough-tui", "--help"],
]
INSTALL_COMMANDS = [
    ["python", "-m", "pip", "install", ".", "pytest==9.1.1"],
    [
        "python",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        "uv==0.12.5",
    ],
]
IMPORT_COMMANDS = [
    [
        "python",
        "-c",
        "import pomodorough.app, pomodorough.cli, pomodorough.tui",
    ]
]
IROH_COMMANDS = [
    ["python", "-m", "pip", "install", ".[iroh]"],
    [
        "python",
        "-c",
        "import iroh; assert len(iroh.SecretKey.generate().to_bytes()) == 32",
    ],
    [
        "QT_QPA_PLATFORM=offscreen",
        "python",
        "-m",
        "unittest",
        "tests.test_iroh_network",
        "-v",
    ],
]
IROH_COMMAND_LINES = [
    '          python -m pip install ".[iroh]"\n',
    (
        '          python -c "import iroh; '
        'assert len(iroh.SecretKey.generate().to_bytes()) == 32"\n'
    ),
    (
        "          QT_QPA_PLATFORM=offscreen python -m unittest "
        "tests.test_iroh_network -v\n"
    ),
]
IROH_STEP_NAMES = [
    "Test optional Iroh runtime",
    "Test optional Iroh runtime on macOS",
]
QT_INSTALL_COMMANDS = [
    ["sudo", "apt-get", "update"],
    [
        "sudo",
        "apt-get",
        "install",
        "--yes",
        "--no-install-recommends",
        "libegl1",
        "libgl1",
        "libxkbcommon-x11-0",
    ],
]
TEST_STEP_NAMES = [
    "Check out source",
    "Verify checked out source",
    "Set up Python",
    "Install Qt runtime libraries",
    "Install package and test runner",
    "Import application entry points",
    "Smoke installed macOS package",
    "Run tests",
    "Retain full pytest evidence",
    "Test optional Iroh runtime",
    "Test optional Iroh runtime on macOS",
]
ROOT_ENV_KEYS = {"CORE_COMMIT", "CORE_RELEASE_TAG", "CORE_SHA256"}


def strip_yaml_comment(value: str) -> str:
    quote = None
    for index, character in enumerate(value):
        if character in "'\"" and (index == 0 or value[index - 1] != "\\"):
            quote = (
                None if quote == character else character if quote is None else quote
            )
        if (
            character == "#"
            and quote is None
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index].rstrip()
    return value.strip()


class WorkflowYaml:
    def __init__(self, source: str) -> None:
        self.lines = source.splitlines()
        self.index = 0

    def parse(self) -> dict[str, Any]:
        self.skip_ignored()
        result = self.parse_block(self.indentation())
        assert isinstance(result, dict), "Workflow root must be a mapping"
        return result

    def skip_ignored(self) -> None:
        while self.index < len(self.lines):
            content = self.lines[self.index].strip()
            if content and not content.startswith("#"):
                return
            self.index += 1

    def indentation(self) -> int:
        line = self.lines[self.index]
        return len(line) - len(line.lstrip())

    def parse_block(self, indentation: int) -> Any:
        self.skip_ignored()
        content = self.lines[self.index].lstrip()
        if content.startswith("- "):
            return self.parse_sequence(indentation)
        return self.parse_mapping(indentation)

    def parse_mapping(
        self, indentation: int, seed: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = {} if seed is None else seed
        while self.index < len(self.lines):
            self.skip_ignored()
            if self.index >= len(self.lines) or self.indentation() < indentation:
                break
            assert self.indentation() == indentation, "Unsupported YAML indentation"
            content = self.lines[self.index].strip()
            if content.startswith("- "):
                break
            key, separator, value = content.partition(":")
            assert separator and key not in result, (
                f"Unsupported YAML mapping entry: {content}"
            )
            self.index += 1
            result[key] = self.parse_mapping_value(indentation, value.strip())
        return result

    def parse_mapping_value(self, indentation: int, value: str) -> Any:
        if value in {"|", "|-", ">", ">-"}:
            return self.parse_literal(
                indentation,
                folded=value.startswith(">"),
                strip=value.endswith("-"),
            )
        if value:
            return parse_scalar(value)
        self.skip_ignored()
        if self.index >= len(self.lines) or self.indentation() <= indentation:
            return None
        return self.parse_block(self.indentation())

    def parse_sequence(self, indentation: int) -> list[Any]:
        result = []
        while self.index < len(self.lines):
            self.skip_ignored()
            if self.index >= len(self.lines) or self.indentation() != indentation:
                break
            content = self.lines[self.index].strip()
            if not content.startswith("- "):
                break
            item = content[2:].strip()
            self.index += 1
            if ":" not in item:
                result.append(parse_scalar(item))
                continue
            key, value = item.split(":", 1)
            mapping = {key: self.parse_mapping_value(indentation + 2, value.strip())}
            result.append(self.parse_mapping(indentation + 2, mapping))
        return result

    def parse_literal(
        self, parent_indentation: int, folded: bool, strip: bool
    ) -> str:
        collected: list[str] = []
        content_indentation = None
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            indentation = len(raw) - len(raw.lstrip()) if raw.strip() else len(raw)
            if raw.strip() and indentation <= parent_indentation:
                break
            self.index += 1
            if not raw.strip():
                collected.append("")
                continue
            content_indentation = content_indentation or indentation
            collected.append(raw[content_indentation:])
        value = (" " if folded else "\n").join(collected).rstrip()
        return value if strip or not collected else f"{value}\n"


def parse_scalar(value: str) -> Any:
    value = strip_yaml_comment(value)
    if value.startswith("[") and value.endswith("]"):
        return [parse_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return ast.literal_eval(value)
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"null", "~"}:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def workflow_model(workflow: str) -> dict[str, Any]:
    return WorkflowYaml(workflow).parse()


def named_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"Expected one {name} step"
    return matches[0]


def assert_step_enforced(step: dict[str, Any], condition: str | None = None) -> None:
    if condition is None:
        assert "if" not in step
    else:
        assert step.get("if") == condition
    assert "continue-on-error" not in step


def assert_step_keys(step: dict[str, Any], expected: set[str]) -> None:
    assert set(step) == expected, f"Unexpected fields on {step.get('name')} step"


def assert_test_step_sequence(job: dict[str, Any]) -> None:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert [step.get("name") for step in steps] == TEST_STEP_NAMES
    suite_index = TEST_STEP_NAMES.index("Run tests")
    evidence_index = TEST_STEP_NAMES.index("Retain full pytest evidence")
    assert evidence_index == suite_index + 1


def assert_suite_environment(model: dict[str, Any], job: dict[str, Any]) -> None:
    assert set(model.get("env", {})) == ROOT_ENV_KEYS
    assert "env" not in job
    for scope in (model, job):
        environment = scope.get("env", {})
        assert isinstance(environment, dict)
        assert all(key.upper() != "PYTEST_ADDOPTS" for key in environment)
    assert "defaults" not in model
    assert "defaults" not in job


def executable_commands(step: dict[str, Any]) -> list[list[str]]:
    script = step.get("run")
    assert isinstance(script, str), "Step must contain shell commands"
    commands = []
    for line in logical_shell_lines(script):
        tokens = shlex.split(line)
        assert tokens and tokens[0] not in {
            "if",
            "then",
            "elif",
            "else",
            "fi",
            "for",
            "while",
            "until",
            "case",
            "esac",
        }, "Critical command cannot be hidden in shell control flow"
        assert not {"||", "&&", "&"}.intersection(tokens), (
            "Critical command cannot be masked"
        )
        commands.append(tokens)
    return commands


def logical_shell_lines(script: str) -> list[str]:
    lines = []
    continued: list[str] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            continued.append(line[:-1].rstrip())
            continue
        continued.append(line)
        lines.append(" ".join(continued))
        continued = []
    assert not continued, "Shell continuation must terminate"
    return lines


def assert_linux_qt_runtime(job: dict[str, Any]) -> None:
    step = named_step(job, "Install Qt runtime libraries")
    assert_step_enforced(step, "runner.os == 'Linux'")
    assert_step_keys(step, {"name", "if", "run"})
    assert executable_commands(step) == QT_INSTALL_COMMANDS


def assert_source_and_matrix(job: dict[str, Any]) -> None:
    assert job.get("needs") == "candidate-source"
    assert job.get("runs-on") == "${{ matrix.os }}"
    assert "if" not in job
    assert "continue-on-error" not in job
    strategy = job["strategy"]
    assert strategy.get("fail-fast") is False
    matrix = strategy["matrix"]
    assert matrix == {
        "os": ["ubuntu-24.04", "macos-15", "windows-2025"],
        "python-version": ["3.11", "3.14"],
    }
    assert_test_step_sequence(job)
    checkout = named_step(job, "Check out source")
    assert_step_enforced(checkout)
    assert_step_keys(checkout, {"name", "uses"})
    assert checkout.get("uses") == CHECKOUT_ACTION
    source_check = named_step(job, "Verify checked out source")
    assert_step_enforced(source_check)
    assert_step_keys(source_check, {"name", "shell", "run"})
    assert source_check.get("shell") == "bash"
    assert executable_commands(source_check) == [
        ["test", "$(git rev-parse HEAD)", "=", "$GITHUB_SHA"]
    ]
    setup = named_step(job, "Set up Python")
    assert_step_enforced(setup)
    assert_step_keys(setup, {"name", "uses", "with"})
    assert setup.get("uses") == SETUP_ACTION
    assert setup.get("with") == {
        "python-version": "${{ matrix.python-version }}",
        "cache": "pip",
    }
    assert_linux_qt_runtime(job)


def assert_macos_package_smoke(job: dict[str, Any]) -> None:
    step = named_step(job, "Smoke installed macOS package")
    assert_step_enforced(step, "runner.os == 'macOS'")
    assert_step_keys(step, {"name", "if", "run"})
    assert executable_commands(step) == [
        ["python", "-m", "pip", "check"],
        *ENTRYPOINT_COMMANDS,
    ]


def assert_full_suite(model: dict[str, Any], job: dict[str, Any]) -> None:
    assert_suite_environment(model, job)
    step = named_step(job, "Run tests")
    assert_step_enforced(step)
    assert_step_keys(step, {"name", "env", "run"})
    assert executable_commands(step) == [shlex.split(FULL_COMMAND)]
    assert step.get("env") == {
        "QT_QPA_PLATFORM": "offscreen",
        "UV_PYTHON": "${{ matrix.python-version }}",
    }
    evidence = named_step(job, "Retain full pytest evidence")
    assert evidence.get("if") == "always()"
    assert "continue-on-error" not in evidence
    assert "run" not in evidence
    assert_step_keys(evidence, {"name", "if", "uses", "with"})
    assert evidence.get("uses") == UPLOAD_ACTION
    assert evidence.get("with") == {
        "name": "pytest-${{ matrix.os }}-python-${{ matrix.python-version }}",
        "path": "pytest-results.xml",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


def assert_macos_iroh(job: dict[str, Any]) -> None:
    step = named_step(job, "Test optional Iroh runtime on macOS")
    assert_step_enforced(step, "runner.os == 'macOS'")
    assert_step_keys(step, {"name", "if", "run"})
    assert executable_commands(step) == IROH_COMMANDS


def assert_cross_platform_iroh(job: dict[str, Any]) -> None:
    step = named_step(job, "Test optional Iroh runtime")
    condition = "runner.os == 'Linux' || runner.os == 'Windows'"
    assert_step_enforced(step, condition)
    assert_step_keys(step, {"name", "if", "shell", "run"})
    assert step.get("shell") == "bash"
    assert executable_commands(step) == IROH_COMMANDS


def assert_macos_runtime_contract(workflow: str) -> None:
    model = workflow_model(workflow)
    job = model["jobs"]["tests"]
    assert_source_and_matrix(job)
    install = named_step(job, "Install package and test runner")
    assert_step_enforced(install)
    assert_step_keys(install, {"name", "run"})
    assert executable_commands(install) == INSTALL_COMMANDS
    imports = named_step(job, "Import application entry points")
    assert_step_enforced(imports)
    assert_step_keys(imports, {"name", "run"})
    assert executable_commands(imports) == IMPORT_COMMANDS
    assert_macos_package_smoke(job)
    assert_full_suite(model, job)
    assert_cross_platform_iroh(job)
    assert_macos_iroh(job)


def test_ci_runs_full_installed_package_suite_on_macos() -> None:
    assert_macos_runtime_contract(WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "ubuntu-24.04, macos-15, windows-2025",
            "ubuntu-24.04, windows-2025",
            id="no-macos",
        ),
        pytest.param(
            "runs-on: ${{ matrix.os }}", "runs-on: ubuntu-24.04", id="fixed-runner"
        ),
        pytest.param("needs: candidate-source", "needs: []", id="no-source-gate"),
        pytest.param(
            FULL_COMMAND,
            FULL_COMMAND.replace("tests -v", "tests -v -k smoke"),
            id="filtered-suite",
        ),
        pytest.param(
            "      - name: Retain full pytest evidence\n"
            "        if: always()\n"
            f"        uses: {UPLOAD_ACTION}\n"
            "        with:\n"
            "          name: pytest-${{ matrix.os }}-python-${{ matrix.python-version }}\n"
            "          path: pytest-results.xml\n"
            "          if-no-files-found: error\n",
            "      - name: Retain full pytest evidence\n"
            "        if: always()\n"
            f"        uses: {UPLOAD_ACTION}\n"
            "        with:\n"
            "          name: pytest-${{ matrix.os }}-python-${{ matrix.python-version }}\n"
            "          path: pytest-results.xml\n"
            "          if-no-files-found: warn\n",
            id="missing-evidence",
        ),
        pytest.param(
            "python -m pip check", "python -m pip --version", id="no-pip-check"
        ),
        pytest.param(
            "if: runner.os == 'macOS'", "if: runner.os == 'Linux'", id="smoke-wrong-os"
        ),
        pytest.param(
            "      - name: Run tests\n        env:\n",
            "      - name: Run tests\n        if: ${{ false }}\n        env:\n",
            id="suite-disabled",
        ),
        pytest.param(
            "      - name: Run tests\n        env:\n",
            "      - name: Run tests\n        continue-on-error: true\n        env:\n",
            id="suite-nonblocking",
        ),
        pytest.param(
            "      - name: Smoke installed macOS package\n        if: runner.os == 'macOS'\n",
            "      - name: Smoke installed macOS package\n        if: ${{ false }}\n",
            id="smoke-disabled",
        ),
        pytest.param(
            "      - name: Smoke installed macOS package\n        if: runner.os == 'macOS'\n",
            "      - name: Smoke installed macOS package\n        if: runner.os == 'macOS' && false\n",
            id="smoke-false-condition",
        ),
        pytest.param(
            "      - name: Smoke installed macOS package\n        if: runner.os == 'macOS'\n",
            "      - name: Smoke installed macOS package\n        if: runner.os == 'macOS'\n        continue-on-error: true\n",
            id="smoke-nonblocking",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS'\n",
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'Linux'\n",
            id="iroh-wrong-os",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS'\n",
            "      - name: Test optional Iroh runtime on macOS\n        if: ${{ false }}\n",
            id="iroh-disabled",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS'\n",
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS' && false\n",
            id="iroh-false-condition",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS'\n",
            "      - name: Test optional Iroh runtime on macOS\n        if: runner.os == 'macOS'\n        continue-on-error: true\n",
            id="iroh-nonblocking",
        ),
    ],
)
def test_macos_runtime_contract_rejects_prior_weakenings(
    before: str, after: str
) -> None:
    assert_mutant_rejected(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            '        python-version: ["3.11", "3.14"]\n',
            '        python-version: ["3.11", "3.14"]\n        exclude:\n          - os: macos-15\n            python-version: "3.11"\n',
            id="exclude-macos-311",
        ),
        pytest.param(
            '        python-version: ["3.11", "3.14"]\n',
            '        python-version: ["3.11", "3.14"]\n        exclude:\n          - os: macos-15\n            python-version: "3.14"\n',
            id="exclude-macos-314",
        ),
        pytest.param(
            "  tests:\n    needs: candidate-source\n",
            "  tests:\n    if: ${{ false }}\n    needs: candidate-source\n",
            id="job-disabled",
        ),
        pytest.param(
            "  tests:\n    needs: candidate-source\n",
            "  tests:\n    continue-on-error: true\n    needs: candidate-source\n",
            id="job-nonblocking",
        ),
        pytest.param(
            "        run: |\n          python -m pip check\n",
            "        run: |\n          if false; then\n            python -m pip check\n          fi\n",
            id="smoke-dead-branch",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime on macOS\n"
            "        if: runner.os == 'macOS'\n"
            "        run: |\n"
            '          python -m pip install ".[iroh]"\n',
            "      - name: Test optional Iroh runtime on macOS\n"
            "        if: runner.os == 'macOS'\n"
            "        run: |\n"
            "          if false; then\n"
            '            python -m pip install ".[iroh]"\n'
            "          fi\n",
            id="iroh-dead-branch",
        ),
        pytest.param(
            f"        run: {FULL_COMMAND}\n",
            f"        run: |\n          if false; then\n            {FULL_COMMAND}\n          fi\n",
            id="suite-dead-branch",
        ),
        pytest.param(
            f"        run: {FULL_COMMAND}\n",
            f"        run: {FULL_COMMAND} || true\n",
            id="suite-masked",
        ),
        pytest.param(
            "          path: pytest-results.xml\n",
            "          path: other-results.xml\n",
            id="wrong-junit-path",
        ),
        pytest.param(
            "      - name: Retain full pytest evidence\n        if: always()\n",
            "      - name: Retain full pytest evidence\n        if: ${{ false }}\n",
            id="upload-disabled",
        ),
        pytest.param(
            "      - name: Retain full pytest evidence\n        if: always()\n",
            "      - name: Retain full pytest evidence\n        if: always()\n        continue-on-error: true\n",
            id="upload-nonblocking",
        ),
        pytest.param(
            f"        uses: {UPLOAD_ACTION}\n        with:\n          name: pytest-",
            "        run: echo upload skipped\n        with:\n          name: pytest-",
            id="upload-echo-only",
        ),
        pytest.param(
            f"      - name: Retain full pytest evidence\n        if: always()\n        uses: {UPLOAD_ACTION}\n",
            "      - name: Retain full pytest evidence\n        if: always()\n        uses: actions/cache@v4\n",
            id="upload-wrong-action",
        ),
    ],
)
def test_macos_runtime_contract_rejects_round_three_mutants(
    before: str, after: str
) -> None:
    assert_mutant_rejected(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  PYTEST_ADDOPTS: -k smoke\n  CORE_COMMIT:",
            id="root-pytest-addopts",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  pytest_addopts: -k smoke\n  CORE_COMMIT:",
            id="root-pytest-addopts-case-variant",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  PYTEST_ADDOPTS: --ignore=tests/test_network.py\n  CORE_COMMIT:",
            id="root-pytest-addopts-ignore",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            'env:\n  PYTEST_ADDOPTS: -m "not integration"\n  CORE_COMMIT:',
            id="root-pytest-addopts-marker-filter",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  PYTEST_ADDOPTS: --deselect=tests/test_network.py\n"
            "  CORE_COMMIT:",
            id="root-pytest-addopts-deselect",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  PYTEST_PLUGINS: tests.narrow_suite\n  CORE_COMMIT:",
            id="root-pytest-plugin",
        ),
        pytest.param(
            "env:\n  CORE_COMMIT:",
            "env:\n  PYTHONPATH: tests/narrow-suite\n  CORE_COMMIT:",
            id="root-pythonpath-injection",
        ),
        pytest.param(
            '"pomodorough-cli": "pomodorough.cli:main"',
            '"pomodorough-cli": "pomodorough.tui:main"',
            id="wrong-entrypoint-target",
        ),
        pytest.param(
            '"pomodorough-cli": "pomodorough.cli:main"',
            '"pomodorough-cli": "pomodorough.cli:missing"',
            id="broken-entrypoint-target",
        ),
        pytest.param(
            "assert all(entries[name].value == target "
            "for name, target in expected.items()); ",
            "",
            id="wrong-entrypoint-target-unchecked",
        ),
        pytest.param(
            "loaded = {name: entries[name].load() for name in expected}",
            "loaded = {name: lambda: None for name in expected}",
            id="broken-entrypoint-target-unloaded",
        ),
        pytest.param(
            "callable(target) and signature(target).bind() is not None",
            "callable(target)",
            id="entrypoint-signature-unchecked",
        ),
        pytest.param(
            "assert target() == 0",
            "assert callable(target)",
            id="gui-entrypoint-not-executed",
        ),
        pytest.param(
            "          pomodorough-cli --help\n",
            "          command -v pomodorough-cli\n",
            id="cli-entrypoint-name-only",
        ),
        pytest.param(
            "          pomodorough-tui --help\n",
            "          command -v pomodorough-tui\n",
            id="tui-entrypoint-name-only",
        ),
        pytest.param(
            "    timeout-minutes: 60\n    strategy:",
            "    timeout-minutes: 60\n    env:\n      PYTEST_ADDOPTS: -k smoke\n    strategy:",
            id="job-pytest-addopts",
        ),
        pytest.param(
            "      - name: Install package and test runner\n        run:",
            "      - name: Install package and test runner\n        if: ${{ false }}\n        run:",
            id="install-disabled",
        ),
        pytest.param(
            "      - name: Install package and test runner\n        run:",
            "      - name: Install package and test runner\n        continue-on-error: true\n        run:",
            id="install-nonblocking",
        ),
        pytest.param(
            "      - name: Import application entry points\n        run:",
            "      - name: Import application entry points\n        if: ${{ false }}\n        run:",
            id="imports-disabled",
        ),
        pytest.param(
            "      - name: Import application entry points\n        run:",
            "      - name: Import application entry points\n"
            "        continue-on-error: true\n"
            "        run:",
            id="imports-nonblocking",
        ),
        pytest.param(
            "if: runner.os == 'Linux' || runner.os == 'Windows'",
            "if: ${{ false }}",
            id="cross-platform-iroh-disabled",
        ),
        pytest.param(
            "if: runner.os == 'Linux' || runner.os == 'Windows'",
            "if: runner.os == 'Linux'",
            id="windows-iroh-omitted",
        ),
        pytest.param(
            "if: runner.os == 'Linux' || runner.os == 'Windows'",
            "if: runner.os == 'Windows'",
            id="linux-iroh-omitted",
        ),
        pytest.param(
            "      - name: Test optional Iroh runtime\n"
            "        if: runner.os == 'Linux' || runner.os == 'Windows'\n"
            "        shell: bash\n",
            "      - name: Test optional Iroh runtime\n"
            "        if: runner.os == 'Linux' || runner.os == 'Windows'\n"
            "        continue-on-error: true\n"
            "        shell: bash\n",
            id="cross-platform-iroh-nonblocking",
        ),
        pytest.param(
            '          python -c "import iroh; assert len(iroh.SecretKey.generate().to_bytes()) == 32"\n',
            "",
            id="cross-platform-iroh-partial",
        ),
        pytest.param(
            '        python-version: ["3.11", "3.14"]\n',
            '        python-version: ["3.11", "3.14"]\n'
            "        exclude:\n"
            "          - os: ubuntu-24.04\n"
            '            python-version: "3.11"\n',
            id="exclude-linux-311",
        ),
        pytest.param(
            '        python-version: ["3.11", "3.14"]\n',
            '        python-version: ["3.11", "3.14"]\n'
            "        exclude:\n"
            "          - os: windows-2025\n"
            '            python-version: "3.14"\n',
            id="exclude-windows-314",
        ),
        pytest.param(
            "            libxkbcommon-x11-0\n",
            "            libxkbcommon-x11-0\n"
            '          echo "PYTEST_ADDOPTS=-k smoke" >> "$GITHUB_ENV"\n',
            id="persisted-pytest-addopts",
        ),
        pytest.param(
            "      - name: Run tests\n        env:\n",
            "      - name: Run tests\n        working-directory: nested\n        env:\n",
            id="junit-working-directory-mismatch",
        ),
    ],
)
def test_macos_runtime_contract_rejects_fixer_mutants(
    before: str, after: str
) -> None:
    assert_mutant_rejected(before, after)


@pytest.mark.parametrize("step_name", IROH_STEP_NAMES)
def test_macos_runtime_contract_rejects_omitted_iroh_step(
    step_name: str,
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mutant = workflow.replace(named_step_source(workflow, step_name), "")
    assert_workflow_rejected(mutant)


@pytest.mark.parametrize("step_name", IROH_STEP_NAMES)
@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(IROH_COMMAND_LINES[0], "", id="install-omitted"),
        pytest.param(IROH_COMMAND_LINES[1], "", id="runtime-probe-omitted"),
        pytest.param(IROH_COMMAND_LINES[2], "", id="tests-omitted"),
        pytest.param(
            IROH_COMMAND_LINES[0],
            "          python -m pip install .\n",
            id="extra-not-installed",
        ),
        pytest.param(
            IROH_COMMAND_LINES[1],
            '          python -c "import iroh"\n',
            id="runtime-probe-weakened",
        ),
        pytest.param(
            IROH_COMMAND_LINES[2],
            "          QT_QPA_PLATFORM=offscreen python -m unittest "
            "tests.test_iroh_network.EndpointKeyStoreTests -v\n",
            id="tests-filtered",
        ),
    ],
)
def test_macos_runtime_contract_rejects_partial_iroh_checks(
    step_name: str, before: str, after: str
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    source = named_step_source(workflow, step_name)
    assert before in source
    weakened = source.replace(before, after, 1)
    assert_workflow_rejected(workflow.replace(source, weakened, 1))


@pytest.mark.parametrize(
    "step_name",
    [
        "Install package and test runner",
        "Import application entry points",
        "Smoke installed macOS package",
    ],
)
def test_macos_runtime_contract_rejects_omitted_package_smoke(
    step_name: str,
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mutant = workflow.replace(named_step_source(workflow, step_name), "")
    assert_workflow_rejected(mutant)


def test_macos_runtime_contract_rejects_upload_before_pytest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mutant = move_named_step_before(workflow, "Retain full pytest evidence", "Run tests")
    assert_workflow_rejected(mutant)


def test_macos_runtime_contract_rejects_smoke_after_pytest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mutant = move_named_step_before(workflow, "Run tests", "Smoke installed macOS package")
    assert_workflow_rejected(mutant)


def test_macos_runtime_contract_rejects_fake_junit_writer() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    evidence = named_step_source(workflow, "Retain full pytest evidence")
    fake_writer = (
        "      - name: Replace pytest evidence\n"
        "        run: printf '<testsuite/>' > pytest-results.xml\n\n"
    )
    assert_workflow_rejected(workflow.replace(evidence, fake_writer + evidence, 1))


def test_macos_runtime_contract_rejects_stale_junit_writer_before_pytest() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suite = named_step_source(workflow, "Run tests")
    fake_writer = (
        "      - name: Precreate pytest evidence\n"
        "        run: printf '<testsuite/>' > pytest-results.xml\n\n"
    )
    assert_workflow_rejected(workflow.replace(suite, fake_writer + suite, 1))


def test_macos_runtime_contract_rejects_post_pytest_junit_rewrite() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suite = named_step_source(workflow, "Run tests")
    rewritten = suite.replace(
        f"        run: {FULL_COMMAND}\n",
        "        run: |\n"
        f"          {FULL_COMMAND}\n"
        "          printf '<testsuite/>' > pytest-results.xml\n",
    )
    assert rewritten != suite
    assert_workflow_rejected(workflow.replace(suite, rewritten, 1))


def named_step_source(workflow: str, name: str) -> str:
    lines = workflow.splitlines(keepends=True)
    marker = f"      - name: {name}\n"
    starts = [index for index, line in enumerate(lines) if line == marker]
    assert len(starts) == 1
    start = starts[0]
    end = start + 1
    while end < len(lines) and not lines[end].startswith("      - name:"):
        if lines[end].strip() and len(lines[end]) - len(lines[end].lstrip()) < 6:
            break
        end += 1
    return "".join(lines[start:end])


def move_named_step_before(workflow: str, moved: str, anchor: str) -> str:
    moved_source = named_step_source(workflow, moved)
    without_moved = workflow.replace(moved_source, "", 1)
    anchor_source = named_step_source(without_moved, anchor)
    return without_moved.replace(anchor_source, moved_source + anchor_source, 1)


def assert_workflow_rejected(workflow: str) -> None:
    with pytest.raises(AssertionError):
        assert_macos_runtime_contract(workflow)


def assert_mutant_rejected(before: str, after: str) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert before in workflow
    assert_workflow_rejected(workflow.replace(before, after, 1))
