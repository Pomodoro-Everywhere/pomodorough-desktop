from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.document = document

    def details(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": str(self),
            "document": self.document,
        }


class TimedDocument(dict[str, Any]):
    def __init__(self, document: dict[str, Any], timing: dict[str, int]) -> None:
        super().__init__(document)
        self.timing = timing


class TokenStorePort(Protocol):
    def load(self) -> dict[str, Any] | None: ...

    def save(self, token_response: dict[str, Any]) -> None: ...

    def clear(self) -> None: ...


Request = Callable[..., dict[str, Any]]
Text = Callable[..., str]


@dataclass
class SessionState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    account_generation: int = 0
    shutting_down: bool = False
    access_token: str | None = None
    refresh_token: str | None = None
    access_expires_at: datetime = field(
        default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc)
    )
    authenticated: bool = False
    busy: bool = False
    deleting_account: bool = False
    sync_queued: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ParsedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime


class AuthenticatedSession:
    def __init__(
        self,
        api_base: str,
        state: SessionState,
        token_store: TokenStorePort,
        request: Request,
        text: Text,
        now: Callable[[], datetime],
        wall_time: Callable[[], float],
        monotonic_ns: Callable[[], int],
    ) -> None:
        self.api_base = api_base
        self.state = state
        self.token_store = token_store
        self.request = request
        self.text = text
        self.now = now
        self.wall_time = wall_time
        self.monotonic_ns = monotonic_ns

    def accept_tokens(self, response: dict[str, Any]) -> None:
        tokens = self._parse_tokens(response)
        self.token_store.save(response)
        self.state.access_token = tokens.access_token
        self.state.refresh_token = tokens.refresh_token
        self.state.access_expires_at = tokens.access_expires_at

    def _parse_tokens(self, response: dict[str, Any]) -> _ParsedTokens:
        try:
            access_token = response["accessToken"]
            access_expiry = response["accessTokenExpiresAt"]
            refresh_token = response["refreshToken"]
            refresh_expiry = response["refreshTokenExpiresAt"]
            values = (access_token, access_expiry, refresh_token, refresh_expiry)
            if not all(isinstance(value, str) for value in values):
                raise TypeError("token fields must be strings")
            access_expires_at = self._parse_expiry(access_expiry)
            refresh_expires_at = self._parse_expiry(refresh_expiry)
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_token_error() from error
        if not self._valid_tokens(
            access_token,
            refresh_token,
            access_expires_at,
            refresh_expires_at,
        ):
            raise self._invalid_token_error()
        return _ParsedTokens(access_token, refresh_token, access_expires_at)

    @staticmethod
    def _parse_expiry(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _valid_tokens(
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> bool:
        return bool(
            access_token.strip()
            and refresh_token.strip()
            and access_expires_at.tzinfo is not None
            and refresh_expires_at.tzinfo is not None
        )

    def _invalid_token_error(self) -> ApiError:
        return ApiError(self.text("network.error.invalid_token"))

    def accept_login_tokens(
        self,
        response: dict[str, Any],
        expected_generation: int | None = None,
    ) -> None:
        with self.state.lock:
            generation = self.generation_or_current(expected_generation)
            self.assert_current_locked(generation)
            self.accept_tokens(response)

    def generation_or_current(self, expected_generation: int | None) -> int:
        return (
            self.state.account_generation
            if expected_generation is None
            else expected_generation
        )

    def assert_current(self, generation: int) -> None:
        with self.state.lock:
            self.assert_current_locked(generation)

    def assert_current_locked(self, generation: int) -> None:
        if self.state.shutting_down or generation != self.state.account_generation:
            raise ApiError(self.text("network.error.sign_in_cancelled"))

    def ensure_access(self, generation: int | None = None) -> str:
        with self.state.lock:
            refresh_generation = self.generation_or_current(generation)
            self.assert_current_locked(refresh_generation)
            if self._access_is_fresh():
                return self.state.access_token or ""
            stored = self.token_store.load()
        refresh_token = stored.get("refreshToken") if stored else None
        if not refresh_token:
            raise ApiError(self.text("network.error.sign_in_required"))
        response = self._refresh(refresh_token, refresh_generation)
        with self.state.lock:
            self.assert_current_locked(refresh_generation)
            self.accept_tokens(response)
            return self.state.access_token or ""

    def _access_is_fresh(self) -> bool:
        return bool(
            self.state.access_token
            and self.state.access_expires_at
            > self.now() + timedelta(seconds=30)
        )

    def _refresh(
        self,
        refresh_token: str,
        generation: int,
    ) -> dict[str, Any]:
        try:
            return self.request(
                "POST",
                f"{self.api_base}/api/v1/auth/refresh",
                {"refreshToken": refresh_token},
            )
        except ApiError as error:
            with self.state.lock:
                try:
                    self.assert_current_locked(generation)
                except ApiError as cancelled:
                    raise cancelled from error
                if error.status == 401:
                    self.token_store.clear()
            raise

    def authorized_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.state.lock:
            generation = self.state.account_generation
        token = self.ensure_access(generation)
        try:
            return self.timed_request(method, path, payload, access_token=token)
        except ApiError as error:
            if error.status != 401:
                raise
            with self.state.lock:
                try:
                    self.assert_current_locked(generation)
                except ApiError as cancelled:
                    raise cancelled from error
                self.state.access_token = None
            token = self.ensure_access(generation)
            return self.timed_request(method, path, payload, access_token=token)

    def timed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        access_token: str,
    ) -> TimedDocument:
        request_physical_ms = int(self.wall_time() * 1000)
        request_monotonic_ms = self.monotonic_ns() // 1_000_000
        document = self.request(
            method,
            f"{self.api_base}{path}",
            payload,
            access_token=access_token,
        )
        received_physical_ms = int(self.wall_time() * 1000)
        received_monotonic_ms = self.monotonic_ns() // 1_000_000
        return TimedDocument(
            document,
            {
                "requestPhysicalMs": request_physical_ms,
                "receivedPhysicalMs": received_physical_ms,
                "requestMonotonicMs": request_monotonic_ms,
                "receivedMonotonicMs": received_monotonic_ms,
            },
        )
