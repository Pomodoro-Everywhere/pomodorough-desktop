from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Self
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.network import (
    _HTTP_RESPONSE_BODY_LIMIT,
    ApiError,
    _request,
)
from pomodorough.network_session import AuthenticatedSession, SessionState


_NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


class _RecordingBody(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        chunk_size: int | None = None,
        on_first_read: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(body)
        self.chunk_size = chunk_size
        self.on_first_read = on_first_read
        self.read_amounts: list[int] = []

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            raise AssertionError("response body read must always be bounded")
        self.read_amounts.append(amount)
        if self.on_first_read is not None:
            callback, self.on_first_read = self.on_first_read, None
            callback()
        if self.chunk_size is not None:
            amount = min(amount, self.chunk_size)
        return super().read(amount)


class _InterruptedBody(_RecordingBody):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.read_completed = False

    def read(self, amount: int = -1) -> bytes:
        if self.read_completed:
            raise OSError("response interrupted")
        self.read_completed = True
        return super().read(amount)


class _RecordingResponse:
    def __init__(
        self,
        body: bytes,
        chunk_size: int | None = None,
        headers: dict[str, str] | None = None,
        stream: _RecordingBody | None = None,
    ) -> None:
        self.stream = stream or _RecordingBody(body, chunk_size)
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.stream.close()

    def read(self, amount: int = -1) -> bytes:
        return self.stream.read(amount)


def _json_body(size: int) -> bytes:
    prefix = b'{"value":"'
    suffix = b'"}'
    return prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix


def _error_body(size: int) -> bytes:
    document = b'{"error":"bounded"}'
    return document + (b" " * (size - len(document)))


def _http_error(
    body: bytes,
    chunk_size: int | None = None,
    *,
    headers: dict[str, str] | None = None,
    status: int = 503,
    stream: _RecordingBody | None = None,
):
    stream = stream or _RecordingBody(body, chunk_size)
    error = urllib.error.HTTPError(
        "https://example.test/fail",
        status,
        "Unavailable",
        headers or {},
        stream,
    )
    return error, stream


def _session(state: SessionState, token_store: Mock) -> AuthenticatedSession:
    return AuthenticatedSession(
        "https://example.test",
        state,
        token_store,
        _request,
        lambda key, **_values: key,
        lambda: _NOW,
        lambda: 0.0,
        lambda: 0,
    )


def _token_document() -> dict[str, str]:
    return {
        "accessToken": "fresh-access",
        "accessTokenExpiresAt": (_NOW + timedelta(minutes=5)).isoformat(),
        "refreshToken": "fresh-refresh",
        "refreshTokenExpiresAt": (_NOW + timedelta(days=30)).isoformat(),
    }


class HTTPResponseBodyBoundsD3Tests(unittest.TestCase):
    def test_success_accepts_exact_limit_without_length_and_closes(self) -> None:
        response = _RecordingResponse(_json_body(_HTTP_RESPONSE_BODY_LIMIT))
        with patch("urllib.request.urlopen", return_value=response):
            document = _request("GET", "https://example.test/items")

        self.assertEqual(len(document["value"]), _HTTP_RESPONSE_BODY_LIMIT - 12)
        self.assertTrue(response.stream.closed)
        self.assertEqual(
            response.stream.read_amounts,
            [_HTTP_RESPONSE_BODY_LIMIT + 1, 1],
        )

    def test_success_rejects_limit_plus_one_and_closes_response(self) -> None:
        valid_prefix = _json_body(_HTTP_RESPONSE_BODY_LIMIT)
        response = _RecordingResponse(
            valid_prefix + b" ", headers={"Content-Length": "1"}
        )
        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(ApiError) as raised,
        ):
            _request("GET", "https://example.test/items")

        self.assertEqual(
            str(raised.exception), "Server returned an invalid JSON response."
        )
        self.assertIsNone(raised.exception.status)
        self.assertTrue(response.stream.closed)
        self.assertEqual(response.stream.read_amounts, [_HTTP_RESPONSE_BODY_LIMIT + 1])

    def test_success_ignores_lying_large_length_for_short_body(self) -> None:
        response = _RecordingResponse(
            b'{"ok":true}', headers={"Content-Length": str(2**63)}
        )
        with patch("urllib.request.urlopen", return_value=response):
            document = _request("GET", "https://example.test/items")

        self.assertEqual(document, {"ok": True})
        self.assertTrue(response.stream.closed)

    def test_http_error_accepts_exact_limit_without_length_and_closes(self) -> None:
        error, stream = _http_error(_error_body(_HTTP_RESPONSE_BODY_LIMIT))
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ApiError) as raised,
        ):
            _request("GET", error.url)

        self.assertEqual(str(raised.exception), "bounded")
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.document, {"error": "bounded"})
        self.assertTrue(stream.closed)
        self.assertEqual(stream.read_amounts, [_HTTP_RESPONSE_BODY_LIMIT + 1, 1])

    def test_http_error_rejects_limit_plus_one_and_closes_response(self) -> None:
        valid_prefix = _error_body(_HTTP_RESPONSE_BODY_LIMIT)
        error, stream = _http_error(
            valid_prefix + b"x", headers={"Content-Length": "1"}
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ApiError) as raised,
        ):
            _request("GET", error.url)

        self.assertEqual(str(raised.exception), "Server returned HTTP 503.")
        self.assertEqual(raised.exception.status, 503)
        self.assertIsNone(raised.exception.document)
        self.assertIs(raised.exception.__cause__, error)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.read_amounts, [_HTTP_RESPONSE_BODY_LIMIT + 1])

    def test_huge_non_json_http_error_is_bounded_and_preserves_401(self) -> None:
        error, stream = _http_error(b"x" * (_HTTP_RESPONSE_BODY_LIMIT * 8), status=401)
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ApiError) as raised,
        ):
            _request("GET", error.url)

        self.assertEqual(str(raised.exception), "Server returned HTTP 401.")
        self.assertEqual(raised.exception.status, 401)
        self.assertIsNone(raised.exception.document)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(stream.read_amounts, [_HTTP_RESPONSE_BODY_LIMIT + 1])
        self.assertTrue(stream.closed)

    def test_short_reads_are_accumulated_for_success_and_error(self) -> None:
        chunked = {"Transfer-Encoding": "chunked"}
        response = _RecordingResponse(
            b'{"ok":true}', chunk_size=2, headers=chunked
        )
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(
                _request("GET", "https://example.test/items"), {"ok": True}
            )
        error, stream = _http_error(
            b'{"error":"short"}', chunk_size=3, headers=chunked
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ApiError) as raised,
        ):
            _request("GET", error.url)

        self.assertEqual(str(raised.exception), "short")
        self.assertGreater(len(response.stream.read_amounts), 2)
        self.assertGreater(len(stream.read_amounts), 2)
        self.assertTrue(response.stream.closed)
        self.assertTrue(stream.closed)

    def test_interrupted_valid_prefix_is_never_decoded_or_accepted(self) -> None:
        success_stream = _InterruptedBody(b'{"ok":true}')
        response = _RecordingResponse(b"", stream=success_stream)
        with (
            patch("urllib.request.urlopen", return_value=response),
            patch("pomodorough.network._decode_success_document") as decode,
            self.assertRaisesRegex(OSError, "response interrupted"),
        ):
            _request("GET", "https://example.test/items")
        error_stream = _InterruptedBody(b'{"error":"partial"}')
        error, _ = _http_error(b"", stream=error_stream)
        with (
            patch("urllib.request.urlopen", side_effect=error),
            patch("pomodorough.network._decode_http_error") as decode_error,
            self.assertRaisesRegex(OSError, "response interrupted"),
        ):
            _request("GET", error.url)

        decode.assert_not_called()
        decode_error.assert_not_called()
        self.assertTrue(success_stream.closed)
        self.assertTrue(error_stream.closed)

    def test_oversized_401_preserves_refresh_retry_contract(self) -> None:
        state = SessionState(
            access_token="stale-access",
            access_expires_at=_NOW + timedelta(minutes=5),
        )
        token_store = Mock()
        token_store.load.return_value = {"refreshToken": "stored-refresh"}
        first_error, error_stream = _http_error(
            b"x" * (_HTTP_RESPONSE_BODY_LIMIT + 1), status=401
        )
        refresh = _RecordingResponse(json.dumps(_token_document()).encode())
        retried = _RecordingResponse(b'{"ok":true}')
        with patch(
            "urllib.request.urlopen",
            side_effect=[first_error, refresh, retried],
        ) as urlopen:
            result = _session(state, token_store).authorized_request(
                "GET", "/api/v1/protected"
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(state.access_token, "fresh-access")
        token_store.save.assert_called_once_with(_token_document())
        self.assertTrue(error_stream.closed)
        self.assertTrue(refresh.stream.closed)
        self.assertTrue(retried.stream.closed)

    def test_oversized_401_cannot_cross_account_generation_fence(self) -> None:
        state = SessionState(
            account_generation=7,
            access_token="stale-access",
            access_expires_at=_NOW + timedelta(minutes=5),
        )
        token_store = Mock()

        def replace_account() -> None:
            with state.lock:
                state.account_generation += 1

        stream = _RecordingBody(
            b"x" * (_HTTP_RESPONSE_BODY_LIMIT + 1),
            on_first_read=replace_account,
        )
        first_error, _ = _http_error(b"", status=401, stream=stream)
        with (
            patch("urllib.request.urlopen", side_effect=first_error) as urlopen,
            self.assertRaisesRegex(ApiError, "network.error.sign_in_cancelled") as raised,
        ):
            _session(state, token_store).authorized_request(
                "GET", "/api/v1/protected"
            )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(raised.exception.__cause__.status, 401)
        token_store.load.assert_not_called()
        token_store.save.assert_not_called()
        self.assertTrue(stream.closed)

    def test_malformed_json_remains_invalid_below_limit(self) -> None:
        response = _RecordingResponse(b"not-json")
        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(ApiError) as success_error,
        ):
            _request("GET", "https://example.test/items")
        error, stream = _http_error(b"not-json")
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ApiError) as http_error,
        ):
            _request("GET", error.url)

        self.assertEqual(
            str(success_error.exception), "Server returned an invalid JSON response."
        )
        self.assertEqual(str(http_error.exception), "Server returned HTTP 503.")
        self.assertTrue(response.stream.closed)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
