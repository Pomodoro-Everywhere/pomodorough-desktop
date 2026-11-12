"""Suite-wide fixtures for the desktop tests."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import lock_file_isolation
import real_secret_store_guard

real_secret_store_guard.install_audit_hook()
lock_file_isolation.WATCH.install()

# R54-D14: one QApplication for the whole process, made before any test
# module runs. A module that only needs a core application then reuses it
# and cannot leave a bare QCoreApplication for a later widget test, which
# Qt aborts on. Test modules default to offscreen the same way.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

APPLICATION = QApplication.instance() or QApplication([])


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "isolated_secret_service: the test starts its own private Secret Service "
        "session bus, so its child processes may run the installed secret-tool.",
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """R54-D12: a credential store hit after the last test still fails the run.

    trylast keeps this after any other session-finish hook that still tears
    fixtures down, so a hit in such a teardown is not missed.
    """
    violations = real_secret_store_guard.STATE.take()
    if not violations:
        return
    message = "Credential store hits recorded after the last test:\n" + "\n".join(violations)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:
        sys.stderr.write(message + "\n")
    if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def real_secret_store_guard_state(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[real_secret_store_guard.GuardState]:
    """R54-D12: no test may read, write or delete a real credential store."""
    state = real_secret_store_guard.STATE
    # A hit recorded since the last teardown came from a leftover thread, a
    # later fixture's teardown or a wider-scoped fixture. Production code may
    # have swallowed it, so it fails this test instead of being dropped.
    leftover = state.take()
    if leftover:
        pytest.fail(
            "Credential store hits recorded between tests:\n" + "\n".join(leftover),
            pytrace=False,
        )
    state.test = request.node.nodeid
    real_secret_store_guard.patch_store_backends(monkeypatch)
    monkeypatch.setenv(real_secret_store_guard.TEST_ENVIRONMENT_KEY, request.node.nodeid)
    if request.node.get_closest_marker("isolated_secret_service") is None:
        child_path = real_secret_store_guard.child_python_path(os.environ.get("PYTHONPATH"))
        monkeypatch.setenv("PYTHONPATH", child_path)
    yield state
    state.test = "<outside a test>"
    # A hit inside a worker thread or a broad handler may not fail the test
    # body, so every recorded hit fails the test here.
    violations = state.take()
    if violations:
        pytest.fail("\n".join(violations), pytrace=False)


@pytest.fixture(autouse=True)
def isolated_lock_folder(
    real_secret_store_guard_state: real_secret_store_guard.GuardState,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """R54-D12 follow-up: every lock file a test opens stays under a temp folder.

    It depends on the secret store guard so the guard reads the real config
    folder before this fixture redirects it.
    """
    watch = lock_file_isolation.WATCH
    base = tmp_path_factory.getbasetemp()
    watch.add_temp_root(base)
    watch.test = request.node.nodeid
    watch.take()
    # mkdtemp picks a fresh name without listing the folder, unlike mktemp.
    profile = Path(tempfile.mkdtemp(prefix="profile-", dir=base))
    # Imported here so collecting the suite does not import the client modules early.
    from pomodorough import network
    from pomodorough.secure_store import PlatformSecretStore

    # The token store puts its fallback lock next to its session file.
    monkeypatch.setattr(network, "user_config_path", lock_file_isolation.fixed_config_path(profile))
    lock_path = lock_file_isolation.rebased_lock_path(PlatformSecretStore._lock_path, profile)
    monkeypatch.setattr(PlatformSecretStore, "_lock_path", lock_path)
    yield profile
    watch.test = "<outside a test>"
    violations = watch.take()
    if violations:
        pytest.fail("\n".join(violations), pytrace=False)
