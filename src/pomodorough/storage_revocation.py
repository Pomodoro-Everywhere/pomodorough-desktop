from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from .secure_store import (
    PlatformSecretStore,
    SecretStore,
    SecureStoreError,
    secret_store_lock,
)

_JOB_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_JOB_LOCKS_GUARD = threading.Lock()


def credential_api_base(value: Any) -> str:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise SecureStoreError("Credential API origin is missing or invalid.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port == 0
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError
    except ValueError:
        raise SecureStoreError("Credential API origin is missing or invalid.") from None
    return value.rstrip("/")


class PendingSessionRevocations:
    """Keep encrypted device/API queues and a durable index of their origins."""

    def __init__(self, secrets: SecretStore, device_id: str) -> None:
        self._secrets = secrets
        self._device_id = device_id
        digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
        self._origins_key = f"oauth-revocation-origins-v1:{digest}"

    def load(self, api_base: str) -> dict[str, dict[str, Any]]:
        with secret_store_lock(self._secrets, self._origins_key):
            return self._load(self._key(api_base))

    @contextmanager
    def claim(self, api_base: str, identifier: str) -> Iterator[None]:
        """Acquire job ownership before the short-lived origin/queue lock."""
        scope = json.dumps([self._key(api_base), identifier]).encode()
        key = f"oauth-revocation-job-v1:{hashlib.sha256(scope).hexdigest()}"
        if isinstance(self._secrets, PlatformSecretStore):
            with self._secrets.lock(key):
                yield
        else:
            with _JOB_LOCKS_GUARD:
                lock = _JOB_LOCKS.setdefault(key, threading.Lock())
            with lock:
                yield

    def update(
        self, api_base: str, identifier: str, credentials: dict[str, Any]
    ) -> None:
        """Update a claimed existing job; replay must never enqueue."""
        with secret_store_lock(self._secrets, self._origins_key):
            key = self._key(api_base)
            pending = self._load(key)
            if identifier not in pending or pending[identifier]["acknowledged"]:
                return
            pending[identifier] = credentials
            self._save(key, pending)

    def enqueue(
        self, api_base: str, identifier: str, credentials: dict[str, Any]
    ) -> None:
        with self.claim(api_base, identifier), secret_store_lock(self._secrets, self._origins_key):
            key = self._key(api_base)
            pending = self._load(key)
            if identifier in pending:
                return
            pending[identifier] = credentials
            self._remember_origin(api_base)
            self._save(key, pending)

    def load_all(self, known_api: str) -> dict[str, dict[str, dict[str, Any]]]:
        with secret_store_lock(self._secrets, self._origins_key):
            origins = self._origins()
            known_api = credential_api_base(known_api)
            origins.add(known_api)
            pending = {api: self._load(self._key(api)) for api in sorted(origins)}
            if pending[known_api]:
                self._remember_origin(known_api)
            return pending

    def save(
        self, api_base: str, identifier: str, credentials: dict[str, Any]
    ) -> None:
        with secret_store_lock(self._secrets, self._origins_key):
            key = self._key(api_base)
            pending = self._load(key)
            pending[identifier] = credentials
            self._remember_origin(api_base)
            self._save(key, pending)

    def acknowledge(self, api_base: str, identifier: str) -> None:
        with secret_store_lock(self._secrets, self._origins_key):
            key = self._key(api_base)
            pending = self._load(key)
            if identifier not in pending:
                return
            del pending[identifier]
            self._save(key, pending)

    def _key(self, api_base: str) -> str:
        scope = json.dumps([self._device_id, credential_api_base(api_base)]).encode()
        return f"oauth-revocations-v1:{hashlib.sha256(scope).hexdigest()}"

    def _origins(self) -> set[str]:
        encoded = self._secrets.load(self._origins_key)
        if encoded is None:
            return set()
        try:
            document = json.loads(encoded)
            if type(document["version"]) is not int or document["version"] != 1:
                raise ValueError
            origins = document["origins"]
            if not isinstance(origins, list):
                raise TypeError
            return {credential_api_base(origin) for origin in origins}
        except (ValueError, TypeError, KeyError):
            raise SecureStoreError("Pending revocation origins are malformed.") from None

    def _remember_origin(self, api_base: str) -> None:
        origins = self._origins()
        api_base = credential_api_base(api_base)
        if api_base not in origins:
            origins.add(api_base)
            encoded = json.dumps({"version": 1, "origins": sorted(origins)}).encode()
            self._secrets.save(self._origins_key, encoded)

    def _load(self, key: str) -> dict[str, dict[str, Any]]:
        encoded = self._secrets.load(key)
        if encoded is None:
            return {}
        try:
            document = json.loads(encoded)
            if type(document["version"]) is not int or document["version"] != 1:
                raise ValueError
            pending = document["pending"]
            if not isinstance(pending, dict) or not all(
                isinstance(identifier, str) and identifier and self._valid(credentials)
                for identifier, credentials in pending.items()
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise SecureStoreError("Pending session revocations are malformed.") from None
        return pending

    @staticmethod
    def _valid(credentials: Any) -> bool:
        if not isinstance(credentials, dict) or not {
            "accessToken", "refreshToken"
        }.issubset(credentials):
            return False
        access = credentials.get("accessToken")
        refresh = credentials.get("refreshToken")
        return (
            (access is None or isinstance(access, str))
            and (refresh is None or isinstance(refresh, str))
            and bool(access or refresh)
            and isinstance(credentials.get("accessTokenIsFresh"), bool)
            and isinstance(credentials.get("acknowledged"), bool)
        )

    def _save(self, key: str, pending: dict[str, dict[str, Any]]) -> None:
        encoded = json.dumps(
            {"version": 1, "pending": pending}, separators=(",", ":")
        ).encode("utf-8")
        self._secrets.save(key, encoded)
