from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from importlib.resources import files
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from pomodorough.network import ApiError, CloudService, _request, _RevisionEventParser


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
    def test_bundled_desktop_client(self) -> None:
        resource = files("pomodorough").joinpath("resources/oauth-client.json")
        config = json.loads(resource.read_text(encoding="utf-8"))["installed"]
        self.assertEqual(
            config["client_id"],
            "614768274539-u8f4a71jko6undhdadku2h7mq200lmt8.apps.googleusercontent.com",
        )
        self.assertNotIn("client_secret", config)


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
