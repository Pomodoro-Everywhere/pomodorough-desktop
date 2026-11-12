"""R54-D14: test order cannot leave a bare QCoreApplication behind.

`test_release_version_user_agent_d6.py` builds a request under
`QCoreApplication.instance() or QCoreApplication([])`. When it ran first in a
process, the next module that asked for `QApplication.instance()` got that
bare core application and Qt aborted at its first widget. The suite
conftest now creates the one QApplication before any test module runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
FIRST = "tests/test_release_version_user_agent_d6.py"
SECOND = "tests/test_review_0400_d70_d76.py"


def _pytest_arguments() -> list[str]:
    arguments = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    # The guarded harness loads the Keychain guard plugin; the child keeps it.
    if "keychain_guard_plugin" in sys.modules:
        arguments += ["-p", "keychain_guard_plugin"]
    return arguments


def test_the_suite_application_is_a_widget_application() -> None:
    assert isinstance(QApplication.instance(), QApplication)


def test_the_user_agent_module_then_a_widget_module_run_in_one_process() -> None:
    completed = subprocess.run(
        [*_pytest_arguments(), FIRST, SECOND],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert "Fatal Python error" not in output, output[-2000:]
    assert completed.returncode == 0, output[-2000:]
