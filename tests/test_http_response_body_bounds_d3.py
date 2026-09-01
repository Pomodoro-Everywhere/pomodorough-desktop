from __future__ import annotations

import io
import os
import unittest
import urllib.error
from typing import Self
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.network import (
    _HTTP_RESPONSE_BODY_LIMIT,
    ApiError,
    _request,
)


class _RecordingBody(io.BytesIO):
    def __init__(self, body: bytes, chunk_size: int | None = None) -> None:
        super().__init__(body)
        self.chunk_size = chunk_size
        self.read_amounts: list[int] = []

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            raise AssertionError("response body read must always be bounded")
        self.read_amounts.append(amount)
        if self.chunk_size is not None:
            amount = min(amount, self.chunk_size)
        return super().read(amount)


class _RecordingResponse:
    def __init__(
        self,
        body: bytes,
        chunk_size: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.stream = _RecordingBody(body, chunk_size)
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
):
    stream = _RecordingBody(body, chunk_size)
    error = urllib.error.HTTPError(
        "https://example.test/fail",
        status,
        "Unavailable",
        headers or {},
        stream,
    )
    return error, stream


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
        response = _RecordingResponse(b'{"ok":true}', chunk_size=2)
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(
                _request("GET", "https://example.test/items"), {"ok": True}
            )
        error, stream = _http_error(b'{"error":"short"}', chunk_size=3)
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
