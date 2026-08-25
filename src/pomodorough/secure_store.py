from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import shutil
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

from platformdirs import user_config_path


class SecureStoreError(OSError):
    pass


class SecretStore(Protocol):
    def load(self, key: str) -> bytes | None: ...

    def save(self, key: str, value: bytes) -> None: ...

    def delete(self, key: str) -> None: ...


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


class PlatformSecretStore:
    _SERVICE = "me.egigoka.pomodorough.iroh"

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_config_path(
            "pomodorough", appauthor=False, roaming=True
        ) / "iroh-secrets-v1"

    def availability(self) -> tuple[bool, str]:
        if os.name == "nt":
            return True, "Windows Data Protection API ready"
        command = "security" if sys_platform() == "darwin" else "secret-tool"
        if shutil.which(command):
            return True, f"{command} secure storage ready"
        return False, f"Iroh requires platform secure storage ({command} was not found)."

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
        command = self._command("find")
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
        encoded = base64.b64encode(value).decode("ascii")
        command = self._command("save")
        input_text = encoded
        if sys_platform() == "darwin":
            command.append(encoded)
            input_text = None
        result = self._run(command, input_text=input_text)
        if result.returncode != 0:
            raise SecureStoreError(
                result.stderr.strip() or "Platform secure storage rejected Iroh credentials."
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
        available, reason = self.availability()
        if not available:
            raise SecureStoreError(reason)
        result = self._run(self._command("delete"))
        if result.returncode != 0:
            raise SecureStoreError(
                result.stderr.strip()
                or "Platform secure storage rejected credential deletion."
            )

    def _command(self, operation: str) -> list[str]:
        if sys_platform() == "darwin":
            if operation == "find":
                return [
                    "security", "find-generic-password", "-s", self._SERVICE,
                    "-a", self._active_key, "-w",
                ]
            if operation == "save":
                return [
                    "security", "add-generic-password", "-U", "-s", self._SERVICE,
                    "-a", self._active_key, "-w",
                ]
            return [
                "security", "delete-generic-password", "-s", self._SERVICE,
                "-a", self._active_key,
            ]
        attributes = ["service", "pomodorough", "kind", "iroh", "key", self._active_key]
        if operation == "find":
            return ["secret-tool", "lookup", *attributes]
        if operation == "save":
            return ["secret-tool", "store", "--label=Pomodorough Iroh", *attributes]
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
        self._active_key = key

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
        raise SecureStoreError("Windows Data Protection API rejected Iroh credentials.")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def sys_platform() -> str:
    import sys

    return sys.platform


__all__ = ["PlatformSecretStore", "SecretMutationJournal", "SecureStoreError"]
