"""Fail closed when a test reaches a real credential store (R54-D12).

Two tests once read the developer's login Keychain because they built the
default OAuth store. This guard turns any such access into a test failure that
names the test and the store key, before the operating system is asked.

It works on two layers:

1. Backend patches, installed per test by ``tests/conftest.py``. They replace
   the macOS Keychain helpers, the Windows DPAPI call and the Windows blob path
   of ``PlatformSecretStore`` and know the key being read, saved or deleted.
2. A process audit hook. It sees every ``ctypes`` library load and symbol
   lookup and every ``subprocess.Popen`` call. It catches the Linux
   ``secret-tool`` path, the legacy ``secret-tool`` migration in
   ``network.py``, the ``security``, KWallet and ``cmdkey`` tools when the
   program itself is the tool, and the Keychain, Secret Service, KWallet and
   Windows Credential Manager libraries when they load through ``ctypes``.
   Child Python processes install the same hook through
   ``real_secret_store_guard_site/sitecustomize.py``.

The hook does not see these routes, so code that takes one can still reach
a store:

- ``subprocess`` with ``shell=True`` or through a wrapper such as ``env``,
  ``xcrun``, ``bash -c`` or ``sudo``, because the audited program is the
  shell or the wrapper;
- ``os.system``, ``os.posix_spawn``, ``os.exec*``, ``os.spawn*`` and
  ``QProcess``;
- D-Bus calls over a socket, with no tool and no ``ctypes`` load;
- ``cmdkey.exe`` and other names with an extension on Windows;
- a child Python started with ``-I``, ``-E`` or ``-S``, or one whose
  ``PYTHONPATH`` puts another ``sitecustomize`` first. A hit inside a child
  also never reaches the state of the parent test process.

A tool that a test wrote into a temporary folder is a fake and stays allowed.
A tool that is not installed at all stays allowed too: the spawn fails with
"not found" and reaches nothing.
"""

from __future__ import annotations

import contextvars
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SITE_DIRECTORY = Path(__file__).resolve().parent / "real_secret_store_guard_site"
TEST_ENVIRONMENT_KEY = "SECRET_STORE_GUARD_TEST"
STORE_TOOLS = frozenset(
    {"security", "secret-tool", "kwallet-query", "kwalletcli", "cmdkey"}
)
STORE_LIBRARY_MARKERS = (
    "security.framework",
    "libsecret",
    "kwallet",
    "kf5wallet",
    "kf6wallet",
    "credui",
)
STORE_SYMBOL_PREFIXES = (
    "SecKeychain",
    "SecItem",
    "CredRead",
    "CredWrite",
    "CredDelete",
    "CredEnumerate",
    "CryptProtectData",
    "CryptUnprotectData",
    "secret_password_",
    "secret_service_",
)
# The argument that names the stored item, per tool. "device" is the
# attribute of the legacy secret-tool item in network.py.
TOOL_KEY_FLAGS = {
    "security": "-a",
    "secret-tool": "key",
    "kwallet-query": "-r",
    "kwalletcli": "-e",
    "cmdkey": "/generic",
}
_AUDIT_EVENTS = frozenset({"subprocess.Popen", "ctypes.dlopen", "ctypes.dlsym"})
# Bound at import so tests that patch shutil.which cannot hide a real tool.
_which = shutil.which
_TEMPORARY_ROOTS = tuple(
    os.path.realpath(root)
    for root in ("/private/var/folders", "/private/tmp", "/tmp", tempfile.gettempdir())
)
_current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "real_secret_store_guard_key", default=None
)


class RealSecretStoreAccess(BaseException):
    """Raised at the store boundary.

    It derives from BaseException so production ``except Exception`` blocks
    cannot turn a guard hit into an ordinary, handled store failure.
    """


class GuardState:
    def __init__(self) -> None:
        self.test = "<outside a test>"
        self.violations: list[str] = []

    def reached(self, store: str, key: str | None) -> RealSecretStoreAccess:
        shown_key = key if key is not None else _current_key.get() or "<unknown>"
        message = (
            f"{self.test} reached a real credential store: {store} "
            f"(key {shown_key!r}). Give the test a fake store."
        )
        self.violations.append(message)
        return RealSecretStoreAccess(message)

    def take(self) -> list[str]:
        taken, self.violations = self.violations, []
        return taken


STATE = GuardState()
_hook_installed = False


def install_audit_hook() -> None:
    global _hook_installed
    if _hook_installed:
        return
    sys.addaudithook(_audit)
    _hook_installed = True


def _audit(event: str, arguments: tuple[Any, ...]) -> None:
    if event not in _AUDIT_EVENTS:
        return
    if event == "subprocess.Popen":
        _check_spawn(arguments[0], arguments[1], arguments[3])
    elif event == "ctypes.dlopen":
        _check_library(arguments[0])
    else:
        _check_symbol(arguments[1])


def _check_library(name: object) -> None:
    text = os.fsdecode(name).lower() if isinstance(name, (str, bytes)) else ""
    if any(marker in text for marker in STORE_LIBRARY_MARKERS):
        raise STATE.reached(f"library load {os.fsdecode(name)}", None)


def _check_symbol(name: object) -> None:
    if isinstance(name, str) and name.startswith(STORE_SYMBOL_PREFIXES):
        raise STATE.reached(f"native call {name}", None)


def _check_spawn(executable: object, arguments: object, environment: object) -> None:
    argv = _argv(arguments)
    program = os.fsdecode(executable) if executable else (argv[0] if argv else "")
    tool = os.path.basename(program)
    if tool not in STORE_TOOLS:
        return
    path = _environment_path(environment)
    resolved = resolve_tool(program, path)
    if resolved is None or os.path.realpath(resolved).startswith(_TEMPORARY_ROOTS):
        return
    raise STATE.reached(f"command {resolved} {' '.join(argv[1:2])}", _tool_key(tool, argv))


def resolve_tool(program: str, path: str | None) -> str | None:
    """Find the executable a spawn would run, on the PATH the spawn uses."""
    if os.sep in program:
        return program if os.path.exists(program) else None
    return _which(program, path=path)


def _argv(arguments: object) -> list[str]:
    if isinstance(arguments, (str, bytes)):
        return os.fsdecode(arguments).split()
    if isinstance(arguments, (list, tuple)):
        return [os.fsdecode(part) if isinstance(part, (str, bytes, os.PathLike)) else str(part)
                for part in arguments]
    return [os.fsdecode(arguments)] if isinstance(arguments, os.PathLike) else []


def _environment_path(environment: object) -> str | None:
    if isinstance(environment, dict):
        value = environment.get("PATH", environment.get(b"PATH"))
        return os.fsdecode(value) if value is not None else os.defpath
    return os.environ.get("PATH")


def _tool_key(tool: str, argv: list[str]) -> str | None:
    for flag in (TOOL_KEY_FLAGS[tool], "device"):
        for position, token in enumerate(argv):
            if token == flag and position + 1 < len(argv):
                return argv[position + 1]
            if token.startswith(f"{flag}:"):
                return token.removeprefix(f"{flag}:")
    return None


@contextmanager
def _key_in_use(key: object) -> Iterator[None]:
    token = _current_key.set(key if isinstance(key, str) else None)
    try:
        yield
    finally:
        _current_key.reset(token)


def _with_key(method: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: Any, key: object, *arguments: Any, **keywords: Any) -> Any:
        with _key_in_use(key):
            return method(self, key, *arguments, **keywords)

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    return guarded


def patch_store_backends(monkeypatch: Any) -> None:
    """Replace every real backend of PlatformSecretStore for one test."""
    from pomodorough import secure_store

    def blocked(store: str) -> Callable[..., Any]:
        def reach(service: str, key: str, *_rest: Any) -> Any:
            raise STATE.reached(f"{store} (service {service})", key)

        return reach

    def framework_load() -> Any:
        raise STATE.reached("Security.framework load", None)

    def dpapi(_secret: bytes, *, protect: bool) -> bytes:
        raise STATE.reached("Windows DPAPI " + ("protect" if protect else "unprotect"), None)

    monkeypatch.setattr(secure_store, "_macos_frameworks", framework_load)
    monkeypatch.setattr(secure_store, "_macos_find", blocked("macOS Keychain lookup"))
    monkeypatch.setattr(secure_store, "_macos_load", blocked("macOS Keychain read"))
    monkeypatch.setattr(secure_store, "_macos_save", blocked("macOS Keychain write"))
    monkeypatch.setattr(secure_store, "_macos_delete", blocked("macOS Keychain delete"))
    monkeypatch.setattr(secure_store, "_windows_crypt", dpapi)
    _patch_windows_blob_path(monkeypatch, secure_store)
    store_class = secure_store.PlatformSecretStore
    for name in ("load", "save", "delete"):
        monkeypatch.setattr(store_class, name, _with_key(getattr(store_class, name)))


def _patch_windows_blob_path(monkeypatch: Any, secure_store: Any) -> None:
    """Windows keeps protected blobs as files under the store root.

    A delete there needs no DPAPI call, so the profile root itself is guarded.
    """
    real_root = Path(
        secure_store.user_config_path("pomodorough", appauthor=False, roaming=True)
    )
    original = secure_store.PlatformSecretStore._windows_path

    def windows_path(store: Any, key: str) -> Path:
        if Path(store.root).is_relative_to(real_root):
            raise STATE.reached(f"Windows protected file under {store.root}", key)
        return original(store, key)

    monkeypatch.setattr(secure_store.PlatformSecretStore, "_windows_path", windows_path)


def child_python_path(current: str | None) -> str:
    """Append the guard site so child Python processes install the hook too.

    Appending keeps any sitecustomize that already sits earlier on the path.
    """
    parts = [part for part in (current or "").split(os.pathsep) if part]
    if str(SITE_DIRECTORY) not in parts:
        parts.append(str(SITE_DIRECTORY))
    return os.pathsep.join(parts)
