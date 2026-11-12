"""R54-D12 follow-up: every lock file a test opens stays under a temp folder.

Without a redirect, the secure store and the token store open their
cross-process lock files under the platform config folder, the same folder
the installed app uses. The conftest fixture moves those lock files to a
per-test temp folder, and this watch reports any lock file that a test
still opens outside a temp folder or inside the real profile.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import platformdirs

LOCK_SUFFIX = ".lock"

# Computed once, before any test redirects the platform folder.
REAL_PROFILE_ROOT = os.path.realpath(
    platformdirs.user_config_path("pomodorough", appauthor=False, roaming=True)
)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _opened_path(argument: object) -> str | None:
    """Return the text of a path argument of the open audit event."""
    if isinstance(argument, bytes):
        return os.fsdecode(argument)
    if isinstance(argument, (str, os.PathLike)):
        return os.fspath(argument)
    # A file descriptor or an unknown type names no path.
    return None


def fixed_config_path(root: Path) -> Callable[..., Path]:
    """Stand in for platformdirs.user_config_path during one test."""

    def config_path(*_arguments: object, **_options: object) -> Path:
        return root

    return config_path


def rebased_lock_path(original: Callable[[Any, str], Path], profile: Path) -> Callable[[Any, str], Path]:
    """Wrap PlatformSecretStore._lock_path so a lock in the real profile moves to `profile`.

    The store root itself keeps its real default, because the secret store
    guard recognizes a real Windows blob by that root and fails closed.
    """

    def lock_path(store: Any, key: str) -> Path:
        path = original(store, key)
        resolved = os.path.realpath(path)
        if not _inside(resolved, REAL_PROFILE_ROOT):
            return path
        return profile / os.path.relpath(resolved, REAL_PROFILE_ROOT)

    return lock_path


class LockFileWatch:
    def __init__(self) -> None:
        self.real_profile_root = REAL_PROFILE_ROOT
        self.temp_roots: tuple[str, ...] = (os.path.realpath(tempfile.gettempdir()),)
        self.test = "<outside a test>"
        self._violations: list[str] = []
        self._guard = threading.Lock()
        self._installed = False

    def install(self) -> None:
        if not self._installed:
            sys.addaudithook(self._audit)
            self._installed = True

    def add_temp_root(self, root: Path) -> None:
        resolved = os.path.realpath(root)
        if resolved not in self.temp_roots:
            self.temp_roots = (*self.temp_roots, resolved)

    def is_isolated(self, path: str) -> bool:
        resolved = os.path.realpath(path)
        if _inside(resolved, self.real_profile_root):
            return False
        return any(_inside(resolved, root) for root in self.temp_roots)

    def observe(self, path: str) -> None:
        if not path.endswith(LOCK_SUFFIX) or self.is_isolated(path):
            return
        with self._guard:
            self._violations.append(
                f"{self.test} opened the lock file {path} outside a temp folder"
            )

    def take(self) -> list[str]:
        with self._guard:
            violations, self._violations = self._violations, []
        return violations

    def _audit(self, event: str, arguments: tuple[object, ...]) -> None:
        if event != "open" or not arguments:
            return
        path = _opened_path(arguments[0])
        if path is not None:
            self.observe(path)


WATCH = LockFileWatch()
