from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_config_path


class SecureStoreError(OSError):
    pass


class TokenCleanupPendingError(SecureStoreError):
    pass


class SecretStore(Protocol):
    def load(self, key: str) -> bytes | None: ...

    def save(self, key: str, value: bytes) -> None: ...

    def delete(self, key: str) -> None: ...


_SECRET_MUTATION_LOCK = threading.RLock()


@contextmanager
def secret_store_lock(store: SecretStore | None, key: str) -> Iterator[None]:
    with _SECRET_MUTATION_LOCK:
        if isinstance(store, PlatformSecretStore):
            with store.lock(key):
                yield
        else:
            yield


@contextmanager
def token_store_lock(store: SecretStore | None, key: str, fallback: Path) -> Iterator[None]:
    fallback = fallback.resolve()
    with secret_store_lock(store, key), _file_lock(fallback.with_name(f".{fallback.name}.lock")):
        yield


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class SecretMutationJournal:
    """Compensate external secret mutations when a surrounding transaction fails."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store
        self._snapshots: dict[str, bytes | None] = {}
        self._order: list[str] = []

    def __enter__(self) -> SecretMutationJournal:
        return self

    def __exit__(self, error_type: object, _error: object, _traceback: object) -> bool:
        if error_type is None:
            return False
        failures: list[BaseException] = []
        for key in reversed(self._order):
            previous = self._snapshots[key]
            try:
                if previous is None:
                    self._store.delete(key)
                else:
                    self._store.save(key, previous)
            except BaseException as error:
                failures.append(error)
        if failures:
            raise SecureStoreError(
                "Secure storage rollback failed after a transaction error."
            ) from failures[0]
        return False

    def save(self, key: str, value: bytes) -> None:
        self._watch(key)
        self._store.save(key, value)

    def delete(self, key: str) -> None:
        self._watch(key)
        self._store.delete(key)

    def _watch(self, key: str) -> None:
        if key in self._snapshots:
            return
        self._snapshots[key] = self._store.load(key)
        self._order.append(key)


_MACOS_ITEM_NOT_FOUND = -25300


@lru_cache(maxsize=1)
def _macos_frameworks() -> tuple[Any, Any]:
    security = ctypes.CDLL(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemDelete.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core.CFRelease.argtypes = [ctypes.c_void_p]
    return security, core


def _macos_find(service: str, key: str) -> tuple[int, bytes | None, ctypes.c_void_p]:
    security, _core = _macos_frameworks()
    service_bytes = service.encode("utf-8")
    key_bytes = key.encode("utf-8")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(key_bytes),
        key_bytes,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    value = ctypes.string_at(data, length.value) if status == 0 else None
    if data.value:
        security.SecKeychainItemFreeContent(None, data)
    return status, value, item


def _macos_release(item: ctypes.c_void_p) -> None:
    if item.value:
        _security, core = _macos_frameworks()
        core.CFRelease(item)


def _macos_require(operation: str, status: int) -> None:
    if status != 0:
        raise SecureStoreError(f"macOS Keychain {operation} failed with status {status}.")


def _macos_load(service: str, key: str) -> bytes | None:
    status, value, item = _macos_find(service, key)
    try:
        if status == _MACOS_ITEM_NOT_FOUND:
            return None
        _macos_require("lookup", status)
        return value
    finally:
        _macos_release(item)


def _macos_save(service: str, key: str, value: bytes) -> None:
    status, _existing, item = _macos_find(service, key)
    security, _core = _macos_frameworks()
    buffer = ctypes.create_string_buffer(value)
    try:
        if status == 0:
            result = security.SecKeychainItemModifyAttributesAndData(
                item, None, len(value), ctypes.cast(buffer, ctypes.c_void_p)
            )
        elif status == _MACOS_ITEM_NOT_FOUND:
            service_bytes = service.encode("utf-8")
            key_bytes = key.encode("utf-8")
            created = ctypes.c_void_p()
            result = security.SecKeychainAddGenericPassword(
                None,
                len(service_bytes),
                service_bytes,
                len(key_bytes),
                key_bytes,
                len(value),
                ctypes.cast(buffer, ctypes.c_void_p),
                ctypes.byref(created),
            )
            _macos_release(created)
        else:
            _macos_require("lookup before save", status)
            return
        _macos_require("save", result)
    finally:
        _macos_release(item)


def _macos_delete(service: str, key: str) -> None:
    status, _value, item = _macos_find(service, key)
    try:
        if status == _MACOS_ITEM_NOT_FOUND:
            return
        _macos_require("lookup before delete", status)
        security, _core = _macos_frameworks()
        _macos_require("delete", security.SecKeychainItemDelete(item))
    finally:
        _macos_release(item)


class PlatformSecretStore:
    _SERVICE = "me.egigoka.pomodorough.iroh"

    def __init__(
        self,
        root: Path | None = None,
        *,
        service: str = _SERVICE,
        kind: str = "iroh",
        label: str = "Pomodorough Iroh",
    ) -> None:
        self.root = root or user_config_path(
            "pomodorough", appauthor=False, roaming=True
        ) / "iroh-secrets-v1"
        self.service = service
        self.kind = kind
        self.label = label

    def _lock_path(self, key: str) -> Path:
        if os.name == "nt":
            root, scope = self.root, key
        else:
            root = user_config_path("pomodorough", appauthor=False, roaming=True)
            namespace = self.service if sys_platform() == "darwin" else self.kind
            scope = f"{sys_platform()}\0{namespace}\0{key}"
        name = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        return root / "secure-store-locks-v1" / f"{name}.lock"

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        self._validate_key(key)
        with _file_lock(self._lock_path(key)):
            yield

    def availability(self) -> tuple[bool, str]:
        if os.name == "nt":
            return True, "Windows Data Protection API ready"
        command = "security" if sys_platform() == "darwin" else "secret-tool"
        if shutil.which(command):
            return True, f"{command} secure storage ready"
        return False, f"{self.label} requires platform secure storage ({command} was not found)."

    def load(self, key: str) -> bytes | None:
        self._validate_key(key)
        if os.name == "nt":
            path = self._windows_path(key)
            try:
                encrypted = path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as error:
                raise SecureStoreError(f"Secure value could not be read: {error}") from error
            return self._windows_unprotect(encrypted)
        if sys_platform() == "darwin":
            return _macos_load(self.service, key)
        command = self._command("find", key)
        result = self._run(command)
        if result.returncode != 0:
            if self._lookup_not_found(result):
                return None
            raise SecureStoreError(
                result.stderr.strip()
                or "Platform secure storage rejected credential lookup."
            )
        encoded = result.stdout.strip()
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise SecureStoreError("Secure storage returned malformed data.") from error

    def save(self, key: str, value: bytes) -> None:
        self._validate_key(key)
        if not isinstance(value, bytes) or not value:
            raise SecureStoreError("Secure value must contain bytes.")
        available, reason = self.availability()
        if not available:
            raise SecureStoreError(reason)
        if os.name == "nt":
            self._write_private(self._windows_path(key), self._windows_protect(value))
            return
        if sys_platform() == "darwin":
            _macos_save(self.service, key, value)
            return
        encoded = base64.b64encode(value).decode("ascii")
        result = self._run(self._command("save", key), input_text=encoded)
        if result.returncode != 0:
            raise SecureStoreError(
                result.stderr.strip()
                or f"Platform secure storage rejected {self.label} credentials."
            )

    def delete(self, key: str) -> None:
        self._validate_key(key)
        if os.name == "nt":
            try:
                self._windows_path(key).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise SecureStoreError(f"Secure value could not be deleted: {error}") from error
            return
        if sys_platform() == "darwin":
            _macos_delete(self.service, key)
            return
        available, reason = self.availability()
        if not available:
            raise SecureStoreError(reason)
        result = self._run(self._command("delete", key))
        if result.returncode != 0:
            raise SecureStoreError(
                result.stderr.strip()
                or "Platform secure storage rejected credential deletion."
            )

    def _command(self, operation: str, key: str) -> list[str]:
        if sys_platform() == "darwin":
            if operation == "find":
                return [
                    "security", "find-generic-password", "-s", self.service,
                    "-a", key, "-w",
                ]
            if operation == "save":
                return [
                    "security", "add-generic-password", "-U", "-s", self.service,
                    "-a", key, "-w",
                ]
            return [
                "security", "delete-generic-password", "-s", self.service,
                "-a", key,
            ]
        attributes = ["service", "pomodorough", "kind", self.kind, "key", key]
        if operation == "find":
            return ["secret-tool", "lookup", *attributes]
        if operation == "save":
            return ["secret-tool", "store", f"--label={self.label}", *attributes]
        return ["secret-tool", "clear", *attributes]

    @staticmethod
    def _lookup_not_found(result: subprocess.CompletedProcess[str]) -> bool:
        if sys_platform() == "darwin":
            return result.returncode == 44
        return result.returncode == 1 and not result.stderr.strip()

    def _run(
        self, command: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SecureStoreError(f"Platform secure storage failed: {error}") from error

    def _validate_key(self, key: str) -> None:
        if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 512:
            raise SecureStoreError("Secure storage key is invalid.")

    def _windows_path(self, key: str) -> Path:
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / name

    @staticmethod
    def _windows_protect(value: bytes) -> bytes:
        return _windows_crypt(value, protect=True)

    @staticmethod
    def _windows_unprotect(value: bytes) -> bytes:
        return _windows_crypt(value, protect=False)

    @staticmethod
    def _write_private(path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_crypt(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise SecureStoreError("Windows Data Protection API is unavailable.")
    source_buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(
        len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    arguments = (
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(target),
    )
    if not function(*arguments):
        raise SecureStoreError("Windows Data Protection API rejected secure credentials.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def sys_platform() -> str:
    import sys

    return sys.platform


__all__ = ["PlatformSecretStore", "SecretMutationJournal", "SecureStoreError"]
