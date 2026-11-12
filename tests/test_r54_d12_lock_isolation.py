"""R54-D12 follow-up: no test may open a lock file in the real profile.

The secure store and the token store place their cross-process lock files
under the platform config folder. The conftest fixture `isolated_lock_folder`
moves those lock files to a per-test temp folder, and `lock_file_isolation.WATCH`
fails any test that still opens a lock file outside a temp folder.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import lock_file_isolation
from pomodorough import network, secure_store
from pomodorough.secure_store import PlatformSecretStore, token_store_lock

WATCH = lock_file_isolation.WATCH


def assert_in_test_profile(path: Path, profile: Path) -> None:
    resolved = Path(os.path.realpath(path))
    assert resolved.is_relative_to(os.path.realpath(profile)), resolved
    assert not resolved.is_relative_to(lock_file_isolation.REAL_PROFILE_ROOT), resolved
    assert WATCH.is_isolated(os.fspath(resolved)), resolved


def test_token_store_config_folder_points_at_the_test_profile(isolated_lock_folder: Path) -> None:
    assert network._config_root() == isolated_lock_folder


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_default_store_lock_lands_in_the_test_profile(
    isolated_lock_folder: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(secure_store, "sys_platform", lambda: platform)
    store = PlatformSecretStore()
    with store.lock("device"):
        path = store._lock_path("device")
        assert path.exists(), path
    assert_in_test_profile(path, isolated_lock_folder)
    assert WATCH.take() == []


def test_default_windows_store_lock_lands_in_the_test_profile(
    isolated_lock_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(secure_store, "os", windows_os)
    # Only the path is checked; locking on Windows needs msvcrt.
    assert_in_test_profile(PlatformSecretStore()._lock_path("device"), isolated_lock_folder)


def test_default_token_store_lock_lands_in_the_test_profile(isolated_lock_folder: Path) -> None:
    fallback = network._config_root() / "session.json"
    with token_store_lock(None, "session", fallback):
        locks = list(isolated_lock_folder.glob("*.lock"))
    assert [path.name for path in locks] == [".session.json.lock"]
    assert_in_test_profile(locks[0], isolated_lock_folder)
    assert WATCH.take() == []


def test_watch_reports_a_lock_opened_in_the_real_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stand-in for the real profile, so the test never writes there.
    profile = tmp_path / "real-profile"
    monkeypatch.setattr(WATCH, "real_profile_root", os.path.realpath(profile))
    inside = profile / "secure-store-locks-v1" / "probe.lock"
    with secure_store._file_lock(inside), secure_store._file_lock(tmp_path / "other.lock"):
        pass
    violations = WATCH.take()
    assert len(violations) == 1, violations
    assert str(inside) in violations[0]


def test_watch_reports_a_lock_opened_outside_every_temp_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    monkeypatch.setattr(WATCH, "temp_roots", (os.path.realpath(allowed),))
    outside = tmp_path / "outside.lock"
    with secure_store._file_lock(allowed / "inside.lock"), secure_store._file_lock(outside):
        (tmp_path / "outside.txt").write_text("not a lock file")
    violations = WATCH.take()
    assert len(violations) == 1, violations
    assert str(outside) in violations[0]


def test_watch_sees_a_lock_opened_by_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "real-profile"
    monkeypatch.setattr(WATCH, "real_profile_root", os.path.realpath(profile))

    def open_lock() -> None:
        with secure_store._file_lock(profile / "worker.lock"):
            pass

    worker = threading.Thread(target=open_lock)
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    violations = WATCH.take()
    assert len(violations) == 1, violations
    assert "worker.lock" in violations[0]
