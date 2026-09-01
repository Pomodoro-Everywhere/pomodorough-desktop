from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from pomodorough.secure_store import SecureStoreError

from pomodorough.network import (
    ApiError,
    CloudService,
    DesktopOAuthContract,
    SystemOAuthBrowserTransport,
    TokenStore,
    _config_root,
    _desktop_oauth_platform,
    _read_oauth_credentials,
    _request,
    _RevisionEventParser,
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            body, self.body = self.body, b""
            return body
        body, self.body = self.body[:amount], self.body[amount:]
        return body


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _FakeRevisionReply:
    def __init__(
        self,
        body: bytes = b"",
        status: int | None = 200,
        content_type: bytes = b"text/event-stream; charset=utf-8",
    ) -> None:
        self.body = body
        self.status = status
        self.content_type = content_type
        self.readyRead = _FakeSignal()
        self.finished = _FakeSignal()
        self.aborted = False
        self.deleted = False

    def readAll(self) -> bytes:
        body, self.body = self.body, b""
        return body

    def attribute(self, _attribute) -> int | None:
        return self.status

    def rawHeader(self, name: str) -> bytes:
        if not isinstance(name, str):
            raise TypeError("QNetworkReply.rawHeader requires a string header name")
        return self.content_type if name.casefold() == "content-type" else b""

    def abort(self) -> None:
        self.aborted = True

    def deleteLater(self) -> None:
        self.deleted = True


class _FakeNetworkManager:
    def __init__(self, reply: _FakeRevisionReply) -> None:
        self.reply = reply
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.reply


class _FakeOAuthBrowser:
    def __init__(
        self,
        callback: dict[str, str] | None = None,
        *,
        error: Exception | None = None,
        redirect_uri: str = "http://127.0.0.1:43123/callback",
    ) -> None:
        self.callback = callback or {}
        self.error = error
        self.redirect_uri = redirect_uri
        self.authorization_urls: list[str] = []
        self.cancelled = False

    def authorize(self, authorization_url):
        self.authorization_urls.append(authorization_url(self.redirect_uri))
        if self.error is not None:
            raise self.error
        return self.redirect_uri, dict(self.callback)

    def cancel(self) -> None:
        self.cancelled = True


class _FakeOAuthHTTPServer:
    def __init__(
        self,
        handler,
        callback: dict[str, str] | None,
    ) -> None:
        self.handler = handler
        self.callback = callback
        self.server_port = 43123
        self.timeout = None
        self.handled = False
        self.closed = False

    def handle_request(self) -> None:
        self.handled = True
        if self.callback is not None:
            self.handler.result_queue.put(dict(self.callback))

    def server_close(self) -> None:
        self.closed = True


def _run_immediately(function, on_result, on_error=None) -> None:
    try:
        on_result(function())
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)


class RevisionEventParserTests(unittest.TestCase):
    def test_parses_chunked_json_and_plain_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(parser.feed(b"event: revision\nda"), [])
        self.assertEqual(
            parser.feed(b'ta: {"revision":12}\n\n: keepalive\n\ndata: 13\n\n'),
            [12, 13],
        )

    def test_ignores_invalid_revision_events(self) -> None:
        parser = _RevisionEventParser()

        self.assertEqual(
            parser.feed(b"data: nope\n\ndata: -1\n\ndata: true\n\n"),
            [],
        )


class OAuthResourceTests(unittest.TestCase):
    def test_config_root_uses_roaming_platform_directory(self) -> None:
        root = Path("platform-config")
        with patch("pomodorough.network.user_config_path", return_value=root) as path:
            self.assertEqual(_config_root(), root)
        path.assert_called_once_with("pomodorough", appauthor=False, roaming=True)

    def test_bundled_desktop_client(self) -> None:
        resource = files("pomodorough").joinpath("resources/oauth-client.json")
        config = json.loads(resource.read_text(encoding="utf-8"))["installed"]
        self.assertEqual(
            config["client_id"],
            "614768274539-a70rconcgcn51ksk37ud352cra2ccb7r.apps.googleusercontent.com",
        )
        self.assertNotIn("client_secret", config)

    def test_credentials_accept_installed_web_and_root_documents(self) -> None:
        variants = (
            {"installed": {"client_id": "installed-client"}},
            {"web": {"client_id": "web-client", "client_secret": "secret"}},
            {"client_id": "root-client", "auth_uri": "https://auth.test"},
        )

        for document in variants:
            with self.subTest(document=document), TemporaryDirectory() as directory:
                source = Path(directory) / "oauth.json"
                source.write_text(json.dumps(document))
                with patch.dict(
                    os.environ,
                    {"POMODOROUGH_GOOGLE_OAUTH_JSON": str(source)},
                ):
                    credentials = _read_oauth_credentials()

                config = document.get("installed") or document.get("web") or document
                self.assertEqual(credentials["client_id"], config["client_id"])
                self.assertEqual(credentials["client_secret"], config.get("client_secret", ""))
                self.assertEqual(
                    credentials["auth_uri"],
                    config.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth"),
                )
                self.assertEqual(
                    credentials["token_uri"],
                    config.get("token_uri", "https://oauth2.googleapis.com/token"),
                )

    def test_user_credentials_precede_bundled_resource(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "google-oauth.json"
            source.write_text(json.dumps({"installed": {"client_id": "user-client"}}))
            with (
                patch.dict(os.environ, {}, clear=False),
                patch("pomodorough.network._config_root", return_value=Path(directory)),
            ):
                os.environ.pop("POMODOROUGH_GOOGLE_OAUTH_JSON", None)
                credentials = _read_oauth_credentials()

        self.assertEqual(credentials["client_id"], "user-client")

    def test_retired_or_secret_bearing_implicit_user_credentials_use_bundle(self) -> None:
        retired_client_id = (
            "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com"
        )
        variants = (
            {"installed": {"client_id": retired_client_id}},
            {"installed": {"client_id": "custom-client", "client_secret": "secret"}},
        )
        for document in variants:
            with self.subTest(document=document), TemporaryDirectory() as directory:
                source = Path(directory) / "google-oauth.json"
                source.write_text(json.dumps(document))
                with (
                    patch.dict(os.environ, {}, clear=False),
                    patch("pomodorough.network._config_root", return_value=Path(directory)),
                ):
                    os.environ.pop("POMODOROUGH_GOOGLE_OAUTH_JSON", None)
                    credentials = _read_oauth_credentials()

                self.assertEqual(
                    credentials["client_id"],
                    "614768274539-a70rconcgcn51ksk37ud352cra2ccb7r.apps.googleusercontent.com",
                )
                self.assertEqual(credentials["client_secret"], "")

    def test_invalid_credentials_report_source_path(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "oauth.json"
            source.write_text("{}")
            with patch.dict(
                os.environ,
                {"POMODOROUGH_GOOGLE_OAUTH_JSON": str(source)},
            ):
                with self.assertRaises(ApiError) as raised:
                    _read_oauth_credentials()

        self.assertIn(str(source), str(raised.exception))


class DesktopOAuthContractTests(unittest.TestCase):
    credentials = {
        "client_id": "desktop-client",
        "client_secret": "desktop-secret",
        "auth_uri": "https://accounts.example.test/authorize",
        "token_uri": "https://accounts.example.test/token",
    }

    def test_authorization_url_has_exact_pkce_state_nonce_and_redirect_contract(self) -> None:
        verifier = "fixed/verifier+value"
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")

        url = DesktopOAuthContract.authorization_url(
            self.credentials,
            "http://127.0.0.1:43123/callback",
            "nonce-value",
            "state-value",
            verifier,
        )

        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(
            urllib.parse.urlunparse(parsed._replace(query="")),
            "https://accounts.example.test/authorize",
        )
        self.assertEqual(
            query,
            {
                "client_id": ["desktop-client"],
                "redirect_uri": ["http://127.0.0.1:43123/callback"],
                "response_type": ["code"],
                "scope": ["openid email profile"],
                "nonce": ["nonce-value"],
                "state": ["state-value"],
                "code_challenge": [expected_challenge],
                "code_challenge_method": ["S256"],
                "prompt": ["select_account"],
            },
        )

    def test_callback_accepts_exact_state_and_rejects_state_errors_and_missing_code(self) -> None:
        self.assertEqual(
            DesktopOAuthContract.authorization_code(
                {"state": "state-value", "code": "  code-value  "},
                "state-value",
            ),
            "code-value",
        )
        cases = (
            ({"state": "wrong", "code": "code"}, "invalid state"),
            (
                {
                    "state": "state-value",
                    "error": "fallback",
                    "error_description": "provider description",
                },
                "provider description",
            ),
            ({"state": "state-value", "error": "access_denied"}, "access_denied"),
            ({"state": "state-value", "code": "   "}, "cancelled"),
        )
        for callback, message in cases:
            with self.subTest(callback=callback), self.assertRaises(ApiError) as raised:
                DesktopOAuthContract.authorization_code(callback, "state-value")
            self.assertIn(message, str(raised.exception))

    def test_token_payload_includes_optional_secret_and_exact_redirect_verifier(self) -> None:
        expected = {
            "client_id": "desktop-client",
            "client_secret": "desktop-secret",
            "code": "code-value",
            "code_verifier": "verifier-value",
            "grant_type": "authorization_code",
            "redirect_uri": "http://127.0.0.1:43123/callback",
        }
        self.assertEqual(
            DesktopOAuthContract.token_payload(
                self.credentials,
                "code-value",
                "http://127.0.0.1:43123/callback",
                "verifier-value",
            ),
            expected,
        )
        without_secret = dict(self.credentials, client_secret="")
        self.assertNotIn(
            "client_secret",
            DesktopOAuthContract.token_payload(
                without_secret,
                "code-value",
                "http://127.0.0.1:43123/callback",
                "verifier-value",
            ),
        )


class SystemOAuthBrowserTransportTests(unittest.TestCase):
    def test_browser_transport_returns_callback_and_always_closes_server(self) -> None:
        created = []

        def create_server(_address, handler):
            server = _FakeOAuthHTTPServer(
                handler,
                {"state": "state-value", "code": "code-value"},
            )
            created.append(server)
            return server

        with (
            patch("pomodorough.network.HTTPServer", side_effect=create_server),
            patch("pomodorough.network.webbrowser.open", return_value=True) as opened,
        ):
            redirect_uri, callback = SystemOAuthBrowserTransport().authorize(
                lambda redirect: f"https://accounts.example.test?redirect={redirect}"
            )

        self.assertEqual(redirect_uri, "http://127.0.0.1:43123/callback")
        self.assertEqual(callback, {"state": "state-value", "code": "code-value"})
        opened.assert_called_once_with(
            "https://accounts.example.test?redirect=http://127.0.0.1:43123/callback",
            new=1,
            autoraise=True,
        )
        self.assertEqual(created[0].timeout, 0.25)
        self.assertTrue(created[0].handled)
        self.assertTrue(created[0].closed)

    def test_browser_transport_reports_open_failure_and_timeout_and_closes_server(self) -> None:
        for opened, callback, message in (
            (False, None, "Could not open"),
            (True, None, "timed out"),
        ):
            with self.subTest(opened=opened):
                created = []

                def create_server(_address, handler):
                    server = _FakeOAuthHTTPServer(handler, callback)
                    created.append(server)
                    return server

                if opened:
                    with (
                        patch(
                            "pomodorough.network.HTTPServer",
                            side_effect=create_server,
                        ),
                        patch(
                            "pomodorough.network.webbrowser.open",
                            return_value=True,
                        ),
                        patch(
                            "pomodorough.network.time.monotonic",
                            side_effect=[0, 181],
                        ),
                        self.assertRaises(ApiError) as raised,
                    ):
                        SystemOAuthBrowserTransport().authorize(
                            lambda _redirect: "https://auth"
                        )
                else:
                    with (
                        patch(
                            "pomodorough.network.HTTPServer",
                            side_effect=create_server,
                        ),
                        patch(
                            "pomodorough.network.webbrowser.open",
                            return_value=False,
                        ),
                        self.assertRaises(ApiError) as raised,
                    ):
                        SystemOAuthBrowserTransport().authorize(
                            lambda _redirect: "https://auth"
                        )

                self.assertIn(message, str(raised.exception))
                self.assertTrue(created[0].closed)
                self.assertFalse(created[0].handled)

    def test_cancel_unblocks_active_callback_listener(self) -> None:
        entered = threading.Event()
        released = threading.Event()
        created = []

        def create_server(_address, handler):
            server = _FakeOAuthHTTPServer(handler, None)

            def handle_request() -> None:
                server.handled = True
                entered.set()
                released.wait(timeout=1)

            server.handle_request = handle_request
            created.append(server)
            return server

        transport = SystemOAuthBrowserTransport()
        errors = []

        def authorize() -> None:
            try:
                transport.authorize(lambda _redirect: "https://auth")
            except Exception as error:
                errors.append(error)

        with (
            patch("pomodorough.network.HTTPServer", side_effect=create_server),
            patch("pomodorough.network.webbrowser.open", return_value=True),
        ):
            worker = threading.Thread(target=authorize)
            worker.start()
            self.assertTrue(entered.wait(timeout=1))
            transport.cancel()
            released.set()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ApiError)
        self.assertEqual(str(errors[0]), "Google sign-in was cancelled.")
        self.assertTrue(created[0].closed)


class DesktopOAuthTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    credentials = {
        "client_id": "desktop-client",
        "client_secret": "desktop-secret",
        "auth_uri": "https://accounts.example.test/authorize",
        "token_uri": "https://accounts.example.test/token",
    }
    token_response = {
        "accessToken": "native-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": "native-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }

    def cloud(self, browser: _FakeOAuthBrowser, generated: list[str]) -> CloudService:
        values = iter(generated)
        cloud = CloudService(
            "device-1",
            "https://example.test",
            oauth_browser=browser,
            token_urlsafe=lambda _size: next(values),
        )
        self.addCleanup(cloud.shutdown)
        return cloud

    def test_full_transaction_preserves_exact_contract_and_persists_after_profile(self) -> None:
        browser = _FakeOAuthBrowser({"state": "state-value", "code": "code-value"})
        cloud = self.cloud(browser, ["state-value", "verifier-value"])
        calls = []

        def request(method, url, payload=None, access_token=None, form=False):
            calls.append((method, url, payload, access_token, form))
            if url.endswith("/challenge"):
                return {"nonce": "nonce-value", "challenge": "native-challenge"}
            if url == self.credentials["token_uri"]:
                return {"id_token": "google-id-token"}
            if url.endswith("/exchange"):
                return dict(self.token_response)
            if url.endswith("/me"):
                return {"user": {"id": "user-1"}}
            self.fail(f"unexpected request: {url}")

        with (
            patch("pomodorough.network._read_oauth_credentials", return_value=self.credentials),
            patch("pomodorough.network._request", side_effect=request),
            patch.object(cloud.token_store, "save") as save,
        ):
            user = cloud._authorize_google()

        self.assertEqual(user, {"id": "user-1"})
        self.assertEqual(len(browser.authorization_urls), 1)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(browser.authorization_urls[0]).query
        )
        self.assertEqual(query["nonce"], ["nonce-value"])
        self.assertEqual(query["state"], ["state-value"])
        self.assertEqual(
            query["code_challenge"],
            [
                base64.urlsafe_b64encode(
                    hashlib.sha256(b"verifier-value").digest()
                ).decode().rstrip("=")
            ],
        )
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "https://example.test/api/v1/auth/google/challenge",
                    {},
                    None,
                    False,
                ),
                (
                    "POST",
                    "https://accounts.example.test/token",
                    {
                        "client_id": "desktop-client",
                        "client_secret": "desktop-secret",
                        "code": "code-value",
                        "code_verifier": "verifier-value",
                        "grant_type": "authorization_code",
                        "redirect_uri": "http://127.0.0.1:43123/callback",
                    },
                    None,
                    True,
                ),
                (
                    "POST",
                    "https://example.test/api/v1/auth/google/exchange",
                    {
                        "idToken": "google-id-token",
                        "challenge": "native-challenge",
                        "deviceId": "device-1",
                        "platform": _desktop_oauth_platform(sys.platform),
                    },
                    None,
                    False,
                ),
                (
                    "GET",
                    "https://example.test/api/v1/me",
                    None,
                    "native-access",
                    False,
                ),
            ],
        )
        save.assert_called_once_with(self.token_response)
        self.assertEqual(cloud.access_token, "native-access")

    def test_stale_authorization_cannot_persist_tokens_or_fetch_profile(self) -> None:
        browser = _FakeOAuthBrowser({"state": "state-value", "code": "code-value"})
        cloud = self.cloud(browser, ["state-value", "verifier-value"])
        calls = []

        def request(method, url, payload=None, access_token=None, form=False):
            calls.append(url)
            if url.endswith("/challenge"):
                return {"nonce": "nonce-value", "challenge": "native-challenge"}
            if url == self.credentials["token_uri"]:
                return {"id_token": "google-id-token"}
            if url.endswith("/exchange"):
                with cloud._lifecycle_lock:
                    cloud._account_generation += 1
                return dict(self.token_response)
            self.fail(f"stale authorization continued to {url}")

        with (
            patch("pomodorough.network._read_oauth_credentials", return_value=self.credentials),
            patch("pomodorough.network._request", side_effect=request),
            patch.object(cloud.token_store, "save") as save,
            self.assertRaisesRegex(ApiError, "cancelled"),
        ):
            cloud._authorize_google(expected_generation=0)

        save.assert_not_called()
        self.assertIsNone(cloud.access_token)
        self.assertFalse(any(url.endswith("/me") for url in calls))

    def test_login_generations_prevent_account_a_from_overwriting_account_b(self) -> None:
        account_a = dict(self.token_response, accessToken="account-a", refreshToken="refresh-a")
        account_b = dict(self.token_response, accessToken="account-b", refreshToken="refresh-b")

        for stale_finishes_last in (False, True):
            with self.subTest(stale_finishes_last=stale_finishes_last):
                cloud = self.cloud(
                    _FakeOAuthBrowser({"state": "state", "code": "code"}),
                    ["state", "verifier"],
                )
                with patch.object(cloud.token_store, "save") as save:
                    if not stale_finishes_last:
                        cloud._accept_login_tokens(account_a, expected_generation=0)
                    with cloud._lifecycle_lock:
                        cloud._account_generation = 1
                        cloud.access_token = None
                        cloud.refresh_token = None
                    cloud._accept_login_tokens(account_b, expected_generation=1)
                    if stale_finishes_last:
                        with self.assertRaisesRegex(ApiError, "cancelled"):
                            cloud._accept_login_tokens(account_a, expected_generation=0)

                self.assertEqual(cloud.access_token, "account-b")
                self.assertEqual(cloud.refresh_token, "refresh-b")
                self.assertEqual(save.call_args_list[-1].args[0], account_b)

    def test_callback_and_google_token_failures_stop_before_native_exchange(self) -> None:
        cases = (
            (
                _FakeOAuthBrowser({"state": "wrong", "code": "code"}),
                {"id_token": "unused"},
                "invalid state",
                1,
            ),
            (
                _FakeOAuthBrowser({"state": "state-value", "code": "code"}),
                {},
                "identity token",
                2,
            ),
        )
        for browser, google_tokens, message, request_count in cases:
            with self.subTest(message=message):
                cloud = self.cloud(browser, ["state-value", "verifier-value"])
                responses = iter(
                    [
                        {"nonce": "nonce", "challenge": "challenge"},
                        google_tokens,
                    ]
                )
                with (
                    patch(
                        "pomodorough.network._read_oauth_credentials",
                        return_value=self.credentials,
                    ),
                    patch(
                        "pomodorough.network._request",
                        side_effect=lambda *_args, **_kwargs: next(responses),
                    ) as request,
                    patch.object(cloud.token_store, "save") as save,
                    self.assertRaises(ApiError) as raised,
                ):
                    cloud._authorize_google()

                self.assertIn(message, str(raised.exception))
                self.assertEqual(request.call_count, request_count)
                save.assert_not_called()
                self.assertIsNone(cloud.access_token)

    def test_native_exchange_failure_does_not_persist_session(self) -> None:
        browser = _FakeOAuthBrowser(
            {"state": "state-value", "code": "code-value"}
        )
        cloud = self.cloud(browser, ["state-value", "verifier-value"])

        def request(_method, url, _payload=None, access_token=None, form=False):
            if url.endswith("/challenge"):
                return {"nonce": "nonce", "challenge": "challenge"}
            if url == self.credentials["token_uri"]:
                return {"id_token": "google-token"}
            if url.endswith("/exchange"):
                raise ApiError("transaction failed", 503)
            self.fail(f"unexpected request: {url}")

        with (
            patch(
                "pomodorough.network._read_oauth_credentials",
                return_value=self.credentials,
            ),
            patch("pomodorough.network._request", side_effect=request),
            patch.object(cloud.token_store, "save") as save,
            self.assertRaises(ApiError) as raised,
        ):
            cloud._authorize_google()

        self.assertEqual(str(raised.exception), "transaction failed")
        save.assert_not_called()
        self.assertIsNone(cloud.access_token)

    def test_profile_failure_keeps_issued_session_for_restore(self) -> None:
        browser = _FakeOAuthBrowser(
            {"state": "state-value", "code": "code-value"}
        )
        cloud = self.cloud(browser, ["state-value", "verifier-value"])

        def request(_method, url, _payload=None, access_token=None, form=False):
            if url.endswith("/challenge"):
                return {"nonce": "nonce", "challenge": "challenge"}
            if url == self.credentials["token_uri"]:
                return {"id_token": "google-token"}
            if url.endswith("/exchange"):
                return dict(self.token_response)
            if url.endswith("/me"):
                self.assertEqual(access_token, "native-access")
                raise ApiError("profile unavailable", 503)
            self.fail(f"unexpected request: {url}")

        with (
            patch(
                "pomodorough.network._read_oauth_credentials",
                return_value=self.credentials,
            ),
            patch("pomodorough.network._request", side_effect=request),
            patch.object(cloud.token_store, "save") as save,
            self.assertRaises(ApiError) as raised,
        ):
            cloud._authorize_google()

        self.assertEqual(str(raised.exception), "profile unavailable")
        save.assert_called_once_with(self.token_response)
        self.assertEqual(cloud.access_token, "native-access")

    def test_malformed_native_token_response_stops_before_profile_and_persistence(self) -> None:
        for response in (
            {},
            dict(self.token_response, accessToken=""),
            dict(self.token_response, accessTokenExpiresAt="not-a-date"),
            dict(self.token_response, accessTokenExpiresAt=123),
            {key: value for key, value in self.token_response.items() if key != "refreshToken"},
        ):
            with self.subTest(response=response):
                browser = _FakeOAuthBrowser(
                    {"state": "state-value", "code": "code-value"}
                )
                cloud = self.cloud(browser, ["state-value", "verifier-value"])

                def request(_method, url, _payload=None, access_token=None, form=False):
                    if url.endswith("/challenge"):
                        return {"nonce": "nonce", "challenge": "challenge"}
                    if url == self.credentials["token_uri"]:
                        return {"id_token": "google-token"}
                    if url.endswith("/exchange"):
                        return response
                    self.fail(f"unexpected request: {url}")

                with (
                    patch(
                        "pomodorough.network._read_oauth_credentials",
                        return_value=self.credentials,
                    ),
                    patch("pomodorough.network._request", side_effect=request),
                    patch.object(cloud.token_store, "save") as save,
                    self.assertRaises(ApiError) as raised,
                ):
                    cloud._authorize_google()

                self.assertEqual(
                    str(raised.exception), "Server returned an invalid token response."
                )
                save.assert_not_called()
                self.assertIsNone(cloud.access_token)

    def test_login_composition_emits_success_and_failure_states(self) -> None:
        success_browser = _FakeOAuthBrowser(
            {"state": "state-value", "code": "code-value"}
        )
        successful = self.cloud(success_browser, ["state-value", "verifier-value"])
        users = []
        statuses = []
        successful.signed_in.connect(users.append)
        successful.status_changed.connect(statuses.append)
        with (
            patch.object(successful, "_start", side_effect=_run_immediately),
            patch.object(successful, "_authorize_google", return_value={"id": "user-1"}),
            patch.object(successful, "start_revision_stream") as stream,
        ):
            successful.login()
        self.assertTrue(successful.authenticated)
        self.assertEqual(users, [{"id": "user-1"}])
        self.assertEqual(statuses, ["WAITING FOR GOOGLE", "SYNC READY"])
        stream.assert_called_once_with()

        failed = self.cloud(
            _FakeOAuthBrowser(error=ApiError("browser failed")),
            ["state-value", "verifier-value"],
        )
        failures = []
        failed_statuses = []
        failed.failure.connect(failures.append)
        failed.status_changed.connect(failed_statuses.append)
        with (
            patch.object(failed, "_start", side_effect=_run_immediately),
            patch.object(failed, "_authorize_google", side_effect=ApiError("browser failed")),
        ):
            failed.login()
        self.assertFalse(failed.authenticated)
        self.assertEqual(failures, ["browser failed"])
        self.assertEqual(
            failed_statuses,
            ["WAITING FOR GOOGLE", "LOCAL • SIGN-IN FAILED"],
        )

    def test_shutdown_cancels_browser_and_blocks_late_token_persistence(self) -> None:
        browser = _FakeOAuthBrowser(
            {"state": "state-value", "code": "code-value"}
        )
        cloud = self.cloud(browser, ["state-value", "verifier-value"])

        cloud.shutdown()
        with (
            patch.object(cloud.token_store, "save") as save,
            self.assertRaises(ApiError) as raised,
        ):
            cloud._accept_login_tokens(self.token_response)

        self.assertEqual(str(raised.exception), "Google sign-in was cancelled.")
        self.assertTrue(browser.cancelled)
        save.assert_not_called()
        self.assertIsNone(cloud.access_token)


class HTTPRequestTests(unittest.TestCase):
    def test_request_encodes_json_headers_bearer_and_timeout(self) -> None:
        from pomodorough import __version__

        response = _FakeHTTPResponse(b'{"ok":true}')
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = _request(
                "POST",
                "https://example.test/items",
                {"title": "A/B"},
                access_token="access-token",
            )

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 20})
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b'{"title":"A/B"}')
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer access-token")
        self.assertEqual(
            request.get_header("User-agent"),
            f"Pomodorough-Desktop/{__version__}",
        )

    def test_request_form_encodes_and_empty_success_returns_object(self) -> None:
        response = _FakeHTTPResponse(b"")
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = _request(
                "POST",
                "https://oauth.test/token",
                {"code": "a+b c"},
                form=True,
            )

        self.assertEqual(result, {})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b"code=a%2Bb+c")
        self.assertEqual(
            request.get_header("Content-type"),
            "application/x-www-form-urlencoded",
        )

    def test_request_rejects_malformed_or_non_object_success_documents(self) -> None:
        for body in (b"not-json", b"[]", b'"string"', b"null"):
            with self.subTest(body=body), patch(
                "urllib.request.urlopen", return_value=_FakeHTTPResponse(body)
            ):
                with self.assertRaises(ApiError) as raised:
                    _request("GET", "https://example.test/items")

            self.assertEqual(
                str(raised.exception), "Server returned an invalid JSON response."
            )
            self.assertIsNone(raised.exception.status)

    def test_request_normalizes_http_error_documents(self) -> None:
        cases = (
            (b'{"error":"fallback","error_description":"preferred"}', "preferred", {"error": "fallback", "error_description": "preferred"}),
            (b'{"error":"fallback"}', "fallback", {"error": "fallback"}),
            (b"not-json", "Server returned HTTP 503.", None),
        )
        for body, message, document in cases:
            with self.subTest(body=body):
                error = urllib.error.HTTPError(
                    "https://example.test/fail",
                    503,
                    "Unavailable",
                    {},
                    io.BytesIO(body),
                )
                with patch("urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(ApiError) as raised:
                        _request("GET", error.url)

                self.assertEqual(str(raised.exception), message)
                self.assertEqual(raised.exception.status, 503)
                self.assertEqual(raised.exception.document, document)
                self.assertIs(raised.exception.__cause__, error)

    def test_request_normalizes_transport_failures_with_cause(self) -> None:
        failures = (
            urllib.error.URLError("dns unavailable"),
            TimeoutError("timed out"),
        )
        for failure in failures:
            with self.subTest(failure=failure), patch(
                "urllib.request.urlopen", side_effect=failure
            ):
                with self.assertRaises(ApiError) as raised:
                    _request("GET", "https://example.test")

            self.assertIn("Could not reach Pomodorough:", str(raised.exception))
            self.assertIs(raised.exception.__cause__, failure)


class AuthenticationNetworkTests(unittest.TestCase):
    def setUp(self) -> None:
        secure_store = _MemorySecretStore()
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("pomodorough.network._config_root", return_value=root))
        self.enterContext(patch("pomodorough.network._oauth_secret_store", return_value=secure_store))

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ensure_access_reuses_fresh_token_without_loading_or_requesting(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "fresh-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        with (
            patch.object(cloud.token_store, "load") as load,
            patch("pomodorough.network._request") as request,
        ):
            self.assertEqual(cloud._ensure_access(), "fresh-access")

        load.assert_not_called()
        request.assert_not_called()

    def test_ensure_access_rejects_missing_refresh_token_without_requesting(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshTokenExpiresAt": "2099-01-02T03:04:05Z"},
            ),
            patch("pomodorough.network._request") as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertEqual(str(raised.exception), "Sign in to sync across devices.")
        request.assert_not_called()

    def test_ensure_access_refreshes_and_persists_rotated_tokens(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        response = {
            "accessToken": "new-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "save") as save,
            patch("pomodorough.network._request", return_value=response) as request,
        ):
            self.assertEqual(cloud._ensure_access(), "new-access")

        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/auth/refresh",
            {"refreshToken": "stored-refresh"},
        )
        save.assert_called_once_with(response)
        self.assertEqual(
            cloud.access_expires_at,
            datetime(2099, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(cloud.access_expires_at.tzinfo)

    def test_stale_refresh_cannot_reinstall_tokens_after_logout(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        response = {
            "accessToken": "stale-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "stale-rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        def refresh_then_logout(*_args: object, **_kwargs: object) -> dict[str, str]:
            cloud.logout()
            return response

        with (
            patch.object(cloud.token_store, "load", return_value={"refreshToken": "old-refresh"}),
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud, "_start_revocation"),
            patch("pomodorough.network._request", side_effect=refresh_then_logout),
        ):
            with self.assertRaisesRegex(ApiError, "cancelled"):
                cloud._ensure_access()

        save.assert_not_called()
        self.assertFalse(cloud.authenticated)
        self.assertIsNone(cloud.access_token)

    def test_stale_refresh_cannot_overwrite_switched_account(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        response = {
            "accessToken": "account-a-stale-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "account-a-stale-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        def refresh_then_switch(*_args: object, **_kwargs: object) -> dict[str, str]:
            cloud.logout()
            cloud.authenticated = True
            cloud.access_token = "account-b-access"
            cloud.refresh_token = "account-b-refresh"
            cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            return response

        with (
            patch.object(cloud.token_store, "load", return_value={"refreshToken": "account-a-refresh"}),
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud, "_start_revocation"),
            patch("pomodorough.network._request", side_effect=refresh_then_switch),
        ):
            with self.assertRaisesRegex(ApiError, "cancelled"):
                cloud._ensure_access()

        save.assert_not_called()
        self.assertTrue(cloud.authenticated)
        self.assertEqual(cloud.access_token, "account-b-access")
        self.assertEqual(cloud.refresh_token, "account-b-refresh")

    def test_stale_refresh_cannot_persist_after_account_deletion_starts(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        store = TokenStore(
            "device-1", secret_store=_MemorySecretStore(),
            fallback_path=root / "session.json",
        )
        cloud = CloudService("device-1", "https://example.test", token_store=store)
        self.addCleanup(cloud.shutdown)
        cloud._accept_tokens({
            "accessToken": "account-access",
            "accessTokenExpiresAt": "2000-01-01T00:00:00Z",
            "refreshToken": "account-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        })
        cloud.authenticated = True
        response = {
            "accessToken": "stale-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "stale-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        def refresh_then_delete(*_args: object, **_kwargs: object) -> dict[str, str]:
            cloud.delete_account("DELETE")
            return response

        with (
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud, "stop_revision_stream"),
            patch.object(QThreadPool.globalInstance(), "start"),
            patch("pomodorough.network._request", side_effect=refresh_then_delete),
        ):
            with self.assertRaisesRegex(ApiError, "cancelled"):
                cloud._ensure_access()

        save.assert_not_called()
        self.assertTrue(cloud.deleting_account)

    def test_stale_refresh_cannot_persist_after_shutdown(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        response = {
            "accessToken": "stale-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "stale-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        def refresh_then_shutdown(*_args: object, **_kwargs: object) -> dict[str, str]:
            cloud.shutdown()
            return response

        with (
            patch.object(cloud.token_store, "load", return_value={"refreshToken": "account-refresh"}),
            patch.object(cloud.token_store, "save") as save,
            patch("pomodorough.network._request", side_effect=refresh_then_shutdown),
        ):
            with self.assertRaisesRegex(ApiError, "cancelled"):
                cloud._ensure_access()

        save.assert_not_called()
        self.assertIsNone(cloud.access_token)

    def test_ensure_access_clears_store_and_reraises_refresh_401(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        error = ApiError("expired refresh", 401)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "clear") as clear,
            patch("pomodorough.network._request", side_effect=error) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertIs(raised.exception, error)
        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/auth/refresh",
            {"refreshToken": "stored-refresh"},
        )
        clear.assert_called_once_with()

    def test_ensure_access_preserves_store_for_non_401_refresh_error(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        error = ApiError("unavailable", 503)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ),
            patch.object(cloud.token_store, "clear") as clear,
            patch("pomodorough.network._request", side_effect=error),
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._ensure_access()

        self.assertIs(raised.exception, error)
        clear.assert_not_called()

    def test_authorized_request_retries_once_after_401_with_refreshed_token(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "stale-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        payload = {"name": "Retained payload"}
        refresh_response = {
            "accessToken": "fresh-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ) as load,
            patch.object(cloud.token_store, "save") as save,
            patch(
                "pomodorough.network._request",
                side_effect=[
                    ApiError("expired access", 401),
                    refresh_response,
                    {"ok": True},
                ],
            ) as request,
        ):
            result = cloud._authorized_request("PUT", "/api/v1/items/7", payload)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "PUT",
                    "https://example.test/api/v1/items/7",
                    payload,
                    access_token="stale-access",
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "stored-refresh"},
                ),
                call(
                    "PUT",
                    "https://example.test/api/v1/items/7",
                    payload,
                    access_token="fresh-access",
                ),
            ],
        )
        load.assert_called_once_with()
        save.assert_called_once_with(refresh_response)

    def test_authorized_request_captures_http_wall_and_monotonic_timing(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "fresh-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        with (
            patch("pomodorough.network.time.time", side_effect=[1_000.0, 1_000.12]),
            patch(
                "pomodorough.network.time.monotonic_ns",
                side_effect=[5_000_000_000, 5_120_000_000],
            ),
            patch(
                "pomodorough.network._request", return_value={"revision": 7}
            ),
        ):
            response = cloud._authorized_request("GET", "/api/v1/bootstrap")

        self.assertEqual(response, {"revision": 7})
        self.assertEqual(
            response.timing,
            {
                "requestPhysicalMs": 1_000_000,
                "receivedPhysicalMs": 1_000_120,
                "requestMonotonicMs": 5_000,
                "receivedMonotonicMs": 5_120,
            },
        )

    def test_authorized_request_propagates_non_401_without_retry(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "fresh-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        error = ApiError("conflict", 409)
        payload = {"expectedRevision": 4}

        with (
            patch.object(cloud.token_store, "load") as load,
            patch("pomodorough.network._request", side_effect=error) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._authorized_request("POST", "/api/v1/sync", payload)

        self.assertIs(raised.exception, error)
        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/sync",
            payload,
            access_token="fresh-access",
        )
        load.assert_not_called()

    def test_authorized_request_consecutive_401s_retry_once_and_propagate(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.access_token = "stale-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        refresh_response = {
            "accessToken": "fresh-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        first_error = ApiError("expired access", 401)
        terminal_error = ApiError("session expired", 401)

        with (
            patch.object(
                cloud.token_store,
                "load",
                return_value={"refreshToken": "stored-refresh"},
            ) as load,
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud.token_store, "clear") as clear,
            patch(
                "pomodorough.network._request",
                side_effect=[first_error, refresh_response, terminal_error],
            ) as request,
        ):
            with self.assertRaises(ApiError) as raised:
                cloud._authorized_request("GET", "/api/v1/protected")

        self.assertIs(raised.exception, terminal_error)
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "GET",
                    "https://example.test/api/v1/protected",
                    None,
                    access_token="stale-access",
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "stored-refresh"},
                ),
                call(
                    "GET",
                    "https://example.test/api/v1/protected",
                    None,
                    access_token="fresh-access",
                ),
            ],
        )
        load.assert_called_once_with()
        save.assert_called_once_with(refresh_response)
        clear.assert_not_called()


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class TokenStoreTests(unittest.TestCase):
    def test_secure_store_roundtrip_survives_restart_without_plaintext_fallback(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        secure_store = _MemorySecretStore()
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ):
            TokenStore("device-1", secret_store=secure_store).save(response)
            restarted = TokenStore("device-1", secret_store=secure_store)

            self.assertEqual(
                restarted.load(),
                {
                    "refreshToken": "refresh-secret",
                    "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
                },
            )
            self.assertFalse(restarted.fallback_path.exists())

    def test_secure_store_clear_deletes_refresh_credential_and_keeps_tombstone(self) -> None:
        secure_store = _MemorySecretStore()
        secure_store.save("oauth:device-1", b'{"refreshToken":"secret"}')
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ):
            store = TokenStore("device-1", secret_store=secure_store)
            store.clear()

            self.assertNotIn("oauth:device-1", secure_store.values)
            self.assertEqual(
                json.loads(store.fallback_path.read_text(encoding="utf-8")),
                {"signedOut": True},
            )

    def test_default_store_uses_platform_oauth_namespace(self) -> None:
        secure_store = _MemorySecretStore()
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ), patch(
            "pomodorough.network.PlatformSecretStore", return_value=secure_store
        ) as platform_store:
            store = TokenStore("device-1")

        platform_store.assert_called_once_with(
            root=Path(directory) / "oauth-secrets-v1",
            service="me.egigoka.pomodorough.oauth",
            kind="oauth",
            label="Pomodorough OAuth",
        )
        self.assertIs(store.secret_store, secure_store)

    def test_default_store_migrates_legacy_secret_tool_item(self) -> None:
        secure_store = _MemorySecretStore()
        legacy = {
            "refreshToken": "legacy-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        lookup = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(legacy), stderr=""
        )
        cleared = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ), patch(
            "pomodorough.network.PlatformSecretStore", return_value=secure_store
        ), patch(
            "pomodorough.network.shutil.which", return_value="/usr/bin/secret-tool"
        ), patch(
            "pomodorough.network.subprocess.run", side_effect=[lookup, cleared]
        ) as run:
            loaded = TokenStore("device-1").load()

        self.assertEqual(loaded, legacy)
        self.assertEqual(
            json.loads(secure_store.values["oauth:device-1"]),
            legacy,
        )
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [
                        "secret-tool",
                        "lookup",
                        "service",
                        "pomodorough",
                        "device",
                        "device-1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                ),
                call(
                    [
                        "secret-tool",
                        "clear",
                        "service",
                        "pomodorough",
                        "device",
                        "device-1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                ),
            ],
        )

    def test_legacy_migration_rolls_back_when_cleanup_is_rejected(self) -> None:
        secure_store = _MemorySecretStore()
        legacy = {"refreshToken": "legacy", "refreshTokenExpiresAt": "2099-01-01Z"}
        lookup = subprocess.CompletedProcess([], 0, json.dumps(legacy), "")
        rejected = subprocess.CompletedProcess([], 1, "", "keyring locked")
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ), patch(
            "pomodorough.network.PlatformSecretStore", return_value=secure_store
        ), patch(
            "pomodorough.network.shutil.which", return_value="/usr/bin/secret-tool"
        ), patch(
            "pomodorough.network.subprocess.run", side_effect=[lookup, rejected]
        ), self.assertRaisesRegex(SecureStoreError, "keyring locked"):
            TokenStore("device-1").load()

        self.assertNotIn("oauth:device-1", secure_store.values)

    def test_legacy_migration_rolls_back_when_cleanup_process_fails(self) -> None:
        secure_store = _MemorySecretStore()
        legacy = {"refreshToken": "legacy", "refreshTokenExpiresAt": "2099-01-01Z"}
        lookup = subprocess.CompletedProcess([], 0, json.dumps(legacy), "")
        with TemporaryDirectory() as directory, patch(
            "pomodorough.network._config_root", return_value=Path(directory)
        ), patch(
            "pomodorough.network.PlatformSecretStore", return_value=secure_store
        ), patch(
            "pomodorough.network.shutil.which", return_value="/usr/bin/secret-tool"
        ), patch(
            "pomodorough.network.subprocess.run",
            side_effect=[lookup, subprocess.TimeoutExpired(["secret-tool"], 10)],
        ), self.assertRaisesRegex(SecureStoreError, "Legacy OAuth credential"):
            TokenStore("device-1").load()

        self.assertNotIn("oauth:device-1", secure_store.values)

    def test_fallback_roundtrip_stores_only_refresh_fields_with_private_mode(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            with (
                patch("pomodorough.network.shutil.which", return_value=None),
                patch("pomodorough.network.subprocess.run") as run,
            ):
                store.save(response)
                loaded = store.load()

            self.assertEqual(loaded, expected)
            self.assertEqual(json.loads(store.fallback_path.read_text()), expected)
            if os.name == "posix":
                self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)
            run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "fchmod"), "requires descriptor chmod")
    def test_fallback_is_private_and_complete_before_atomic_replace(self) -> None:
        response = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        real_replace = os.replace

        for existing_mode in (None, 0o644):
            with self.subTest(existing_mode=existing_mode), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1", secret_store=None)
                store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
                if existing_mode is not None:
                    store.fallback_path.write_text("previous")
                    store.fallback_path.chmod(existing_mode)
                replacements = []

                def checked_replace(source: str | Path, destination: str | Path) -> None:
                    source_path = Path(source)
                    replacements.append(
                        (
                            source_path.stat().st_mode & 0o777,
                            json.loads(source_path.read_text()),
                            store.fallback_path.read_text()
                            if store.fallback_path.exists()
                            else None,
                        )
                    )
                    real_replace(source, destination)

                with (
                    patch("pomodorough.network.shutil.which", return_value=None),
                    patch("pomodorough.network.os.replace", side_effect=checked_replace),
                ):
                    store.save(response)

                self.assertEqual(len(replacements), 1)
                mode, document, previous = replacements[0]
                if os.name == "posix":
                    self.assertEqual(mode, 0o600)
                self.assertEqual(document, response)
                self.assertEqual(
                    previous, "previous" if existing_mode is not None else None
                )
                if os.name == "posix":
                    self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)

    def test_valid_keyring_json_loads_keyring(self) -> None:
        expected = {
            "refreshToken": "keyring-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0, stdout=json.dumps(expected)),
                ),
            ):
                self.assertEqual(store.load(), expected)

    def test_fallback_from_failed_rotation_takes_precedence_over_stale_keyring(self) -> None:
        fallback = {
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        stale_keyring = {
            "refreshToken": "stale-refresh",
            "refreshTokenExpiresAt": "2099-01-01T00:00:00Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text(json.dumps(fallback))
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0, stdout=json.dumps(stale_keyring)),
                ) as run,
            ):
                self.assertEqual(store.load(), fallback)

            run.assert_not_called()

    def test_malformed_or_non_object_keyring_json_returns_no_session(self) -> None:
        for stdout in ("{malformed", json.dumps(["not", "an", "object"])):
            with self.subTest(stdout=stdout), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1", secret_store=None)
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run",
                        return_value=Mock(returncode=0, stdout=stdout),
                    ) as run,
                ):
                    self.assertIsNone(store.load())

                run.assert_called_once_with(
                    [
                        "secret-tool",
                        "lookup",
                        "service",
                        "pomodorough",
                        "device",
                        "device-1",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

    def test_keyring_load_process_failures_return_no_session(self) -> None:
        failures = (
            subprocess.TimeoutExpired(["secret-tool"], 10),
            OSError("secret service unavailable"),
        )

        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1", secret_store=None)
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run", side_effect=failure
                    ),
                ):
                    self.assertIsNone(store.load())

    def test_failed_keyring_store_writes_fallback(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=1),
                ) as run,
            ):
                store.save(response)

            self.assertEqual(json.loads(store.fallback_path.read_text()), expected)
        run.assert_called_once_with(
            [
                "secret-tool",
                "store",
                "--label=Pomodorough",
                "service",
                "pomodorough",
                "device",
                "device-1",
            ],
            input=json.dumps(expected, separators=(",", ":")),
            text=True,
            timeout=15,
            check=False,
        )

    def test_keyring_store_process_failures_write_fallback(self) -> None:
        response = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        failures = (
            subprocess.TimeoutExpired(["secret-tool"], 15),
            OSError("secret service unavailable"),
        )

        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1", secret_store=None)
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch(
                        "pomodorough.network.subprocess.run", side_effect=failure
                    ),
                ):
                    store.save(response)

                self.assertEqual(json.loads(store.fallback_path.read_text()), response)

    def test_successful_keyring_store_replaces_then_removes_stale_fallback(self) -> None:
        response = {
            "accessToken": "access-secret",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        expected = {
            "refreshToken": "refresh-secret",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")
            replacements = []
            real_replace = os.replace

            def checked_replace(source: str | Path, destination: str | Path) -> None:
                replacements.append(json.loads(Path(source).read_text()))
                real_replace(source, destination)

            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch("pomodorough.network.os.replace", side_effect=checked_replace),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                store.save(response)

            self.assertFalse(store.fallback_path.exists())
            self.assertEqual(replacements, [expected])
        run.assert_called_once_with(
            [
                "secret-tool",
                "store",
                "--label=Pomodorough",
                "service",
                "pomodorough",
                "device",
                "device-1",
            ],
            input=json.dumps(expected, separators=(",", ":")),
            text=True,
            timeout=15,
            check=False,
        )

    def test_clear_removes_keyring_and_keeps_tombstone_idempotently(self) -> None:
        command = [
            "secret-tool",
            "clear",
            "service",
            "pomodorough",
            "device",
            "device-1",
        ]

        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")
            with (
                patch(
                    "pomodorough.network.shutil.which",
                    return_value="/usr/bin/secret-tool",
                ),
                patch(
                    "pomodorough.network.subprocess.run",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                store.clear()
                store.clear()

            self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})
        self.assertEqual(
            run.call_args_list,
            [
                call(command, timeout=10, check=False),
                call(command, timeout=10, check=False),
            ],
        )

    def test_clear_tombstones_failed_keyring_deletion(self) -> None:
        failures = (
            Mock(returncode=1),
            subprocess.TimeoutExpired(["secret-tool"], 10),
            OSError("secret service unavailable"),
        )
        for failure in failures:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                with patch(
                    "pomodorough.network._config_root", return_value=Path(directory)
                ):
                    store = TokenStore("device-1", secret_store=None)
                store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
                store.fallback_path.write_text("stale")
                run_kwargs = (
                    {"return_value": failure}
                    if isinstance(failure, Mock)
                    else {"side_effect": failure}
                )
                with (
                    patch(
                        "pomodorough.network.shutil.which",
                        return_value="/usr/bin/secret-tool",
                    ),
                    patch("pomodorough.network.subprocess.run", **run_kwargs) as run,
                ):
                    store.clear()
                    self.assertIsNone(store.load())

                self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})
                if os.name == "posix":
                    self.assertEqual(store.fallback_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(run.call_count, 1)

    def test_clear_without_keyring_tombstones_session(self) -> None:
        with TemporaryDirectory() as directory:
            with patch(
                "pomodorough.network._config_root", return_value=Path(directory)
            ):
                store = TokenStore("device-1", secret_store=None)
            store.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            store.fallback_path.write_text("stale")

            with (
                patch("pomodorough.network.shutil.which", return_value=None),
                patch("pomodorough.network.subprocess.run") as run,
            ):
                store.clear()
                self.assertIsNone(store.load())

            run.assert_not_called()
            self.assertEqual(json.loads(store.fallback_path.read_text()), {"signedOut": True})


class RevisionStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def cloud_with_reply(self, reply: _FakeRevisionReply):
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        cloud.access_token = "stream-access"
        manager = _FakeNetworkManager(reply)
        cloud._network = manager
        return cloud, manager

    def test_start_builds_authenticated_stream_and_ignores_duplicate(self) -> None:
        reply = _FakeRevisionReply()
        cloud, manager = self.cloud_with_reply(reply)
        cloud._revision_reconnect.start()

        cloud.start_revision_stream()
        cloud.start_revision_stream()

        self.assertEqual(len(manager.requests), 1)
        request = manager.requests[0]
        self.assertEqual(request.url().toString(), "https://example.test/api/v1/stream")
        self.assertEqual(bytes(request.rawHeader("Accept")), b"text/event-stream")
        self.assertEqual(
            bytes(request.rawHeader("Authorization")),
            b"Bearer stream-access",
        )
        self.assertFalse(cloud._revision_reconnect.isActive())
        self.assertEqual(len(reply.readyRead.callbacks), 1)
        self.assertEqual(len(reply.finished.callbacks), 1)

    def test_ready_read_emits_chunked_revisions_and_stale_reply_is_ignored(self) -> None:
        reply = _FakeRevisionReply(b'data: {"revision":17}\n\ndata: 18\n\n')
        cloud, _manager = self.cloud_with_reply(reply)
        revisions = []
        cloud.revision_available.connect(revisions.append)
        cloud.start_revision_stream()

        reply.readyRead.callbacks[0]()
        cloud._read_revision_stream(_FakeRevisionReply(b"data: 99\n\n"))

        self.assertEqual(revisions, [17, 18])

    def test_204_and_non_sse_responses_never_emit_revisions(self) -> None:
        for reply in (
            _FakeRevisionReply(b"data: 21\n\n", status=204),
            _FakeRevisionReply(
                b"data: 22\n\n", content_type=b"application/json"
            ),
            _FakeRevisionReply(b"data: 23\n\n", content_type=b""),
        ):
            with self.subTest(status=reply.status, content_type=reply.content_type):
                cloud, _manager = self.cloud_with_reply(reply)
                revisions = []
                cloud.revision_available.connect(revisions.append)
                cloud.start_revision_stream()

                reply.readyRead.callbacks[0]()
                reply.finished.callbacks[0]()

                self.assertEqual(revisions, [])
                self.assertTrue(cloud._revision_reconnect.isActive())
                cloud._revision_reconnect.stop()

    def test_reconnect_backoff_is_jittered_bounded_and_resets_after_valid_data(self) -> None:
        reply = _FakeRevisionReply(status=503)
        cloud, _manager = self.cloud_with_reply(reply)
        cloud.start_revision_stream()

        with patch("pomodorough.network.secrets.randbelow", return_value=250):
            reply.finished.callbacks[0]()
        self.assertEqual(cloud._revision_reconnect.interval(), 1_250)

        cloud._revision_reconnect.stop()
        second = _FakeRevisionReply(status=503)
        cloud._network = _FakeNetworkManager(second)
        cloud.start_revision_stream()
        with patch("pomodorough.network.secrets.randbelow", return_value=500):
            second.finished.callbacks[0]()
        self.assertEqual(cloud._revision_reconnect.interval(), 2_500)

        cloud._revision_reconnect.stop()
        valid = _FakeRevisionReply(b"data: 30\n\n")
        cloud._network = _FakeNetworkManager(valid)
        cloud.start_revision_stream()
        valid.readyRead.callbacks[0]()
        self.assertEqual(cloud._revision_reconnect_attempt, 0)

        cloud._revision_reply = None
        cloud._revision_reconnect_attempt = 20
        with patch("pomodorough.network.secrets.randbelow", return_value=10_000):
            cloud._schedule_revision_reconnect()
        self.assertLessEqual(cloud._revision_reconnect.interval(), 30_000)

    def test_unauthorized_finish_clears_access_and_does_not_reconnect(self) -> None:
        reply = _FakeRevisionReply(status=401)
        cloud, _manager = self.cloud_with_reply(reply)
        stale = []
        cloud.authorization_stale.connect(lambda: stale.append(True))
        cloud.start_revision_stream()

        reply.finished.callbacks[0]()

        self.assertEqual(stale, [True])
        self.assertIsNone(cloud.access_token)
        self.assertFalse(cloud._revision_reconnect.isActive())
        self.assertTrue(reply.deleted)

    def test_non_401_finish_reconnects_unless_shutdown_or_signed_out(self) -> None:
        for shutdown, authenticated in ((False, True), (True, True), (False, False)):
            with self.subTest(shutdown=shutdown, authenticated=authenticated):
                reply = _FakeRevisionReply(status=503)
                cloud, _manager = self.cloud_with_reply(reply)
                cloud.start_revision_stream()
                cloud._shutting_down = shutdown
                cloud.authenticated = authenticated

                reply.finished.callbacks[0]()

                self.assertEqual(
                    cloud._revision_reconnect.isActive(),
                    not shutdown and authenticated,
                )
                cloud._revision_reconnect.stop()

    def test_stale_finish_only_deletes_stale_reply(self) -> None:
        active = _FakeRevisionReply()
        stale = _FakeRevisionReply()
        cloud, _manager = self.cloud_with_reply(active)
        cloud.start_revision_stream()

        cloud._revision_stream_finished(stale)

        self.assertTrue(stale.deleted)
        self.assertIs(cloud._revision_reply, active)

    def test_stop_resets_parser_and_disposes_reply_idempotently(self) -> None:
        reply = _FakeRevisionReply(b"data: 12")
        cloud, _manager = self.cloud_with_reply(reply)
        cloud.start_revision_stream()
        cloud._revision_parser.feed(b"data: 11")

        cloud.stop_revision_stream()
        cloud.stop_revision_stream()

        self.assertIsNone(cloud._revision_reply)
        self.assertEqual(cloud._revision_parser.buffer, b"")
        self.assertTrue(reply.aborted)
        self.assertTrue(reply.deleted)


class CloudOrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def cloud(self) -> CloudService:
        root = Path(self.enterContext(TemporaryDirectory()))
        cloud = CloudService(
            "device-1", "https://example.test",
            token_store=TokenStore(
                "device-1", secret_store=_MemorySecretStore(),
                fallback_path=root / "session.json",
            ),
        )
        self.addCleanup(cloud.shutdown)
        return cloud

    def test_restore_without_session_stays_local_without_profile_request(self) -> None:
        cloud = self.cloud()
        statuses = []
        cloud.status_changed.connect(statuses.append)
        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud.token_store, "load", return_value=None),
            patch("pomodorough.network._request") as request,
        ):
            cloud.restore()

        self.assertEqual(statuses, ["CONNECTING", "LOCAL • SIGN IN TO SYNC"])
        self.assertFalse(cloud.authenticated)
        request.assert_not_called()

    def test_restore_valid_session_signs_in_and_starts_stream(self) -> None:
        cloud = self.cloud()
        user = {"id": "user-1"}
        users = []
        statuses = []
        cloud.signed_in.connect(users.append)
        cloud.status_changed.connect(statuses.append)
        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
            patch.object(cloud, "_ensure_access", return_value="access"),
            patch("pomodorough.network._request", return_value={"user": user}) as request,
            patch.object(cloud, "start_revision_stream") as stream,
        ):
            cloud.restore()

        self.assertTrue(cloud.authenticated)
        self.assertEqual(users, [user])
        self.assertEqual(statuses, ["CONNECTING", "SYNC READY"])
        request.assert_called_once_with(
            "GET",
            "https://example.test/api/v1/me",
            access_token="access",
        )
        stream.assert_called_once_with()

    def test_restore_401_expires_session_and_generic_error_reports_offline(self) -> None:
        for error, expected_status, expires in (
            (ApiError("expired", 401), "SESSION EXPIRED • SIGN IN AGAIN", True),
            (ApiError("unavailable", 503), "OFFLINE • RETRYING", False),
        ):
            with self.subTest(error=error):
                cloud = self.cloud()
                statuses = []
                failures = []
                expired = []
                cloud.status_changed.connect(statuses.append)
                cloud.failure.connect(failures.append)
                cloud.session_expired.connect(lambda: expired.append(True))
                with (
                    patch.object(cloud, "_start", side_effect=_run_immediately),
                    patch.object(cloud.token_store, "load", return_value={"refreshToken": "stored"}),
                    patch.object(cloud, "_ensure_access", side_effect=error),
                    patch.object(cloud.token_store, "clear") as clear,
                ):
                    cloud.restore()

                self.assertEqual(statuses[-1], expected_status)
                self.assertEqual(expired, [True] if expires else [])
                self.assertEqual(failures, [] if expires else [str(error)])
                self.assertEqual(clear.call_count, 1 if expires else 0)
                self.assertFalse(cloud.authenticated)

    def test_busy_sync_keeps_latest_payload_and_finished_dispatches_once(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.busy = True
        first = {"lastRevision": 1}
        latest = {"lastRevision": 2}
        worker = Mock()
        cloud._workers.add(worker)

        cloud.sync(first)
        cloud.sync(latest)
        with patch.object(cloud, "sync") as sync:
            cloud._finished(worker)

        sync.assert_called_once_with(latest)
        self.assertIsNone(cloud._sync_queued)
        self.assertFalse(cloud.busy)

    def test_sync_guards_success_and_failures(self) -> None:
        payload = {"lastRevision": 4}
        unauthenticated = self.cloud()
        with patch.object(unauthenticated, "_start") as start:
            unauthenticated.sync(payload)
        start.assert_not_called()

        successful = self.cloud()
        successful.authenticated = True
        responses = []
        statuses = []
        successful.sync_ready.connect(responses.append)
        successful.status_changed.connect(statuses.append)
        with (
            patch.object(successful, "_start", side_effect=_run_immediately),
            patch.object(successful, "_authorized_request", return_value={"revision": 5}) as request,
            patch.object(successful, "start_revision_stream") as stream,
        ):
            successful.sync(payload)
        request.assert_called_once_with("POST", "/api/v1/sync", payload)
        self.assertEqual(responses, [{"revision": 5}])
        self.assertEqual(statuses, ["SYNCING", "SYNCED"])
        stream.assert_called_once_with()

        for error, expected_status, expires in (
            (ApiError("expired", 401), "SESSION EXPIRED • SIGN IN AGAIN", True),
            (ApiError("down", 503), "OFFLINE • RETRYING", False),
        ):
            with self.subTest(error=error):
                failed = self.cloud()
                failed.authenticated = True
                statuses = []
                failures = []
                failed.status_changed.connect(statuses.append)
                failed.failure.connect(failures.append)
                with (
                    patch.object(failed, "_start", side_effect=_run_immediately),
                    patch.object(failed, "_authorized_request", side_effect=error),
                    patch.object(failed.token_store, "clear"),
                ):
                    failed.sync(payload)
                self.assertEqual(statuses[-1], expected_status)
                self.assertEqual(failures, [] if expires else [str(error)])

    def test_bootstrap_guards_success_and_preserved_failure(self) -> None:
        cloud = self.cloud()
        with patch.object(cloud, "_start") as start:
            cloud.preview_bootstrap()
            cloud.resolve_bootstrap({"requestId": "request-1"})
        start.assert_not_called()

        cloud.authenticated = True
        previews = []
        resolved = []
        statuses = []
        cloud.bootstrap_ready.connect(previews.append)
        cloud.bootstrap_resolved.connect(resolved.append)
        cloud.status_changed.connect(statuses.append)
        payload = {"requestId": "request-1"}
        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(
                cloud,
                "_authorized_request",
                side_effect=[{"revision": 4}, {"revision": 5}],
            ) as request,
            patch.object(cloud, "start_revision_stream") as stream,
        ):
            cloud.preview_bootstrap()
            cloud.resolve_bootstrap(payload)

        self.assertEqual(previews, [{"revision": 4}])
        self.assertEqual(resolved, [{"revision": 5}])
        self.assertEqual(
            request.call_args_list,
            [
                call("GET", "/api/v1/bootstrap"),
                call("POST", "/api/v1/bootstrap/resolve", payload),
            ],
        )
        self.assertEqual(statuses, ["CHECKING HISTORY", "HISTORY DECISION", "RESOLVING HISTORY", "SYNCED"])
        stream.assert_called_once_with()

        error = ApiError("unavailable", 503)
        failures = []
        cloud.failure.connect(failures.append)
        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud, "_authorized_request", side_effect=error),
        ):
            cloud.resolve_bootstrap(payload)
        self.assertEqual(failures, [str(error)])
        self.assertEqual(statuses[-2:], ["RESOLVING HISTORY", "OFFLINE • HISTORY PRESERVED"])

    def test_logout_during_busy_work_clears_local_session_immediately(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "logout-access"
        cloud.busy = True
        cloud._sync_queued = {"lastRevision": 9}
        signed_out = []
        cloud.signed_out.connect(lambda: signed_out.append(True))

        with (
            patch.object(cloud, "stop_revision_stream") as stop,
            patch.object(cloud.token_store, "clear") as clear,
            patch.object(cloud, "_start_revocation") as revoke,
        ):
            cloud.logout()

        stop.assert_called_once_with()
        clear.assert_called_once_with()
        revoke.assert_called_once_with(
            "logout-access",
            refresh_token=None,
            access_token_is_fresh=False,
            identifier=ANY,
            api_base="https://example.test",
        )
        self.assertFalse(cloud.authenticated)
        self.assertFalse(cloud.busy)
        self.assertIsNone(cloud.access_token)
        self.assertIsNone(cloud._sync_queued)
        self.assertEqual(signed_out, [True])

    def test_shutdown_invalidates_in_flight_worker_callbacks(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "shutdown-access"
        sync_results = []
        failures = []
        cloud.sync_ready.connect(sync_results.append)
        cloud.failure.connect(failures.append)

        with patch.object(QThreadPool.globalInstance(), "start"):
            cloud.sync({"lastRevision": 1})
            worker = next(iter(cloud._workers))
            cloud.shutdown()
            worker.signals.result.emit({"revision": 2})
            worker.signals.error.emit(ApiError("late failure"))
            worker.signals.finished.emit()

        self.assertEqual(sync_results, [])
        self.assertEqual(failures, [])
        self.assertFalse(cloud.busy)
        self.assertIsNone(cloud._sync_queued)

    def test_logout_generation_ignores_old_sync_and_allows_immediate_new_login(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "old-access"
        sync_results = []
        cloud.sync_ready.connect(sync_results.append)

        with patch.object(QThreadPool.globalInstance(), "start") as start:
            cloud.sync({"lastRevision": 1})
            old_worker = next(iter(cloud._workers))
            with (
                patch.object(cloud.token_store, "clear"),
                patch.object(cloud, "_start_revocation"),
            ):
                cloud.logout()
            cloud.login()
            self.assertEqual(start.call_count, 2)
            self.assertTrue(cloud.busy)

            old_worker.signals.result.emit({"revision": 99})
            old_worker.signals.finished.emit()

        self.assertEqual(sync_results, [])
        self.assertTrue(cloud.busy)
        self.assertFalse(cloud.authenticated)

    def test_offline_logout_clears_locally_before_best_effort_revocation(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "logout-access"
        signed_out = []
        statuses = []
        cloud.signed_out.connect(lambda: signed_out.append(True))
        cloud.status_changed.connect(statuses.append)
        with (
            patch.object(cloud, "stop_revision_stream") as stop,
            patch.object(cloud, "_start_revocation") as revoke,
            patch.object(cloud.token_store, "clear") as clear,
        ):
            cloud.logout()

        stop.assert_called_once_with()
        clear.assert_called_once_with()
        revoke.assert_called_once_with(
            "logout-access",
            refresh_token=None,
            access_token_is_fresh=False,
            identifier=ANY,
            api_base="https://example.test",
        )
        self.assertFalse(cloud.authenticated)
        self.assertIsNone(cloud.access_token)
        self.assertEqual(signed_out, [True])
        self.assertEqual(statuses, ["LOCAL • SIGN IN TO SYNC"])

    def test_logout_refreshes_expired_captured_session_then_revokes_without_persisting(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "expired-access"
        cloud.refresh_token = "captured-refresh"
        cloud.access_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        refreshed = {
            "accessToken": "revocation-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "rotated-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }

        with (
            patch.object(QThreadPool.globalInstance(), "start") as start,
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch("pomodorough.network._request", side_effect=[refreshed, {}]) as request,
        ):
            cloud.logout()
            worker = start.call_args.args[0]
            cloud.authenticated = True
            cloud.access_token = "new-account-access"
            cloud.refresh_token = "new-account-refresh"
            worker.function()

        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "captured-refresh"},
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/logout",
                    {},
                    access_token="revocation-access",
                ),
            ],
        )
        save.assert_not_called()
        self.assertTrue(cloud.authenticated)
        self.assertEqual(cloud.access_token, "new-account-access")
        self.assertEqual(cloud.refresh_token, "new-account-refresh")

    def test_logout_refresh_failure_is_contained_and_cannot_touch_new_session(self) -> None:
        cloud = self.cloud()
        cloud.authenticated = True
        cloud.access_token = "expired-access"
        cloud.refresh_token = "captured-refresh"
        cloud.access_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        retries = []

        with (
            patch.object(QThreadPool.globalInstance(), "start") as start,
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch.object(
                QTimer,
                "singleShot",
                side_effect=lambda delay, callback: retries.append((delay, callback)),
            ),
            patch(
                "pomodorough.network._request",
                side_effect=ApiError("refresh unavailable", 503),
            ) as request,
        ):
            cloud.logout()
            worker = start.call_args.args[0]
            cloud.authenticated = True
            cloud.access_token = "new-account-access"
            cloud.refresh_token = "new-account-refresh"
            worker.run()

        request.assert_called_once_with(
            "POST",
            "https://example.test/api/v1/auth/refresh",
            {"refreshToken": "captured-refresh"},
        )
        self.assertEqual(len(retries), 1)
        save.assert_not_called()
        self.assertTrue(cloud.authenticated)
        self.assertEqual(cloud.access_token, "new-account-access")
        self.assertEqual(cloud.refresh_token, "new-account-refresh")

    def test_delete_account_requires_exact_confirmation_and_success_clears_session(self) -> None:
        cloud = self.cloud()
        cloud._accept_tokens({
            "accessToken": "delete-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "delete-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        })
        cloud.authenticated = True
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        deleted = []
        cloud.account_deleted.connect(lambda: deleted.append(True))
        exact_clear = cloud.token_store.clear_account_deletion_credentials_locked

        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud, "stop_revision_stream") as stop,
            patch.object(
                cloud.token_store,
                "clear_account_deletion_credentials_locked",
                wraps=exact_clear,
            ) as clear,
            patch("pomodorough.network._request", return_value={}) as request,
        ):
            for confirmation in ("", "delete", "DELETE "):
                cloud.delete_account(confirmation)
            request.assert_not_called()
            cloud.delete_account("DELETE")

        request.assert_called_once_with(
            "DELETE",
            "https://example.test/api/v1/account",
            {"confirmation": "DELETE"},
            access_token="delete-access",
        )
        stop.assert_called_once_with()
        clear.assert_called_once()
        self.assertEqual(deleted, [True])
        self.assertFalse(cloud.authenticated)
        self.assertFalse(cloud.deleting_account)
        self.assertIsNone(cloud.access_token)

    def test_delete_account_failure_preserves_session_and_resumes_stream(self) -> None:
        cloud = self.cloud()
        cloud._accept_tokens({
            "accessToken": "preserved-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "preserved-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        })
        cloud.authenticated = True
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        failures = []
        cloud.account_deletion_failed.connect(failures.append)

        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud, "stop_revision_stream"),
            patch.object(cloud, "start_revision_stream") as start_stream,
            patch.object(cloud.token_store, "clear") as clear,
            patch(
                "pomodorough.network._request",
                side_effect=ApiError("offline", 503),
            ),
        ):
            cloud.delete_account("DELETE")

        clear.assert_not_called()
        start_stream.assert_called_once_with()
        self.assertEqual(failures, ["offline"])
        self.assertTrue(cloud.authenticated)
        self.assertFalse(cloud.deleting_account)
        self.assertEqual(cloud.access_token, "preserved-access")

    def test_stale_account_a_delete_success_cannot_clear_account_b(self) -> None:
        cloud = self.cloud()
        cloud._accept_tokens({
            "accessToken": "account-a",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "account-a-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        })
        cloud.authenticated = True
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        deleted = []
        cloud.account_deleted.connect(lambda: deleted.append(True))

        with (
            patch.object(QThreadPool.globalInstance(), "start"),
            patch("pomodorough.network._request", return_value={}) as request,
        ):
            cloud.busy = True  # Simulate an in-flight account-A sync.
            cloud.delete_account("DELETE")
            deletion_worker = next(
                worker
                for worker, generation in cloud._worker_generations.items()
                if generation == cloud._account_generation
            )
            with (
                patch.object(cloud.token_store, "clear"),
                patch.object(cloud, "_start_revocation"),
            ):
                cloud.logout()
            cloud.authenticated = True
            cloud.access_token = "account-b"
            with self.assertRaisesRegex(ApiError, "cancelled") as cancelled:
                deletion_worker.function()
            deletion_worker.signals.error.emit(cancelled.exception)
            deletion_worker.signals.result.emit({})
            deletion_worker.signals.finished.emit()

        request.assert_not_called()
        self.assertEqual(deleted, [])
        self.assertTrue(cloud.authenticated)
        self.assertEqual(cloud.access_token, "account-b")


class BootstrapNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _run_immediately(function, on_result, on_error=None) -> None:
        try:
            on_result(function())
        except Exception as error:
            if on_error is None:
                raise
            on_error(error)

    def test_request_preserves_structured_409_response(self) -> None:
        document = {"error": "revision_conflict", "actualRevision": 4}
        http_error = urllib.error.HTTPError(
            "https://example.test/api/v1/bootstrap/resolve",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(document).encode()),
        )

        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ApiError) as raised:
                _request("POST", http_error.url, {})

        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.document, document)
        self.assertEqual(raised.exception.details()["status"], 409)

    def test_cloud_exposes_bootstrap_preview_and_structured_conflict(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        preview = {"revision": 4}
        preview_results = []
        conflicts = []
        cloud.bootstrap_ready.connect(preview_results.append)
        cloud.bootstrap_conflict.connect(conflicts.append)

        with (
            patch.object(cloud, "_start", side_effect=self._run_immediately),
            patch.object(cloud, "_authorized_request", return_value=preview) as call,
        ):
            cloud.preview_bootstrap()
        call.assert_called_once_with("GET", "/api/v1/bootstrap")
        self.assertEqual(preview_results, [preview])

        conflict = ApiError(
            "revision conflict", 409, {"error": "revision_conflict"}
        )
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        with (
            patch.object(cloud, "_start", side_effect=self._run_immediately),
            patch.object(cloud, "_authorized_request", side_effect=conflict) as call,
        ):
            cloud.resolve_bootstrap(payload)
        call.assert_called_once_with(
            "POST", "/api/v1/bootstrap/resolve", payload
        )
        self.assertEqual(conflicts[0]["status"], 409)
        self.assertEqual(conflicts[0]["document"]["error"], "revision_conflict")
        cloud.shutdown()

    def test_worker_preserves_409_type_for_bootstrap_conflict(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        conflict = ApiError(
            "revision conflict", 409, {"error": "revision_conflict"}
        )
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        conflicts = []
        failures = []
        loop = QEventLoop()

        def received(details: dict[str, object]) -> None:
            conflicts.append(details)
            loop.quit()

        cloud.bootstrap_conflict.connect(received)
        cloud.failure.connect(failures.append)
        with patch.object(cloud, "_authorized_request", side_effect=conflict):
            cloud.resolve_bootstrap(payload)
            QTimer.singleShot(2_000, loop.quit)
            loop.exec()

        QApplication.processEvents()
        self.assertEqual(failures, [])
        self.assertEqual(conflicts[0]["status"], 409)
        self.assertEqual(conflicts[0]["document"], {"error": "revision_conflict"})
        cloud.shutdown()

    def test_terminal_bootstrap_401_expires_session_through_worker(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        cloud.authenticated = True
        cloud.access_token = "expired-token"
        payload = {
            "requestId": "request-1",
            "deviceId": "device-1",
            "expectedRevision": 4,
            "strategy": "merge",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        signed_out = []
        session_expired = []
        failures = []
        statuses = []
        loop = QEventLoop()
        cloud.signed_out.connect(lambda: signed_out.append(True))
        cloud.session_expired.connect(
            lambda: (session_expired.append(True), loop.quit())
        )
        cloud.failure.connect(failures.append)
        cloud.status_changed.connect(statuses.append)

        with (
            patch.object(
                cloud,
                "_authorized_request",
                side_effect=ApiError("session expired", 401),
            ),
            patch.object(cloud.token_store, "clear") as clear,
        ):
            cloud.resolve_bootstrap(payload)
            QTimer.singleShot(2_000, loop.quit)
            loop.exec()

        QApplication.processEvents()
        clear.assert_called_once_with()
        self.assertEqual(signed_out, [])
        self.assertEqual(session_expired, [True])
        self.assertEqual(failures, [])
        self.assertFalse(cloud.authenticated)
        self.assertIsNone(cloud.access_token)
        self.assertIn("SESSION EXPIRED • SIGN IN AGAIN", statuses)
        cloud.shutdown()


if __name__ == "__main__":
    unittest.main()
