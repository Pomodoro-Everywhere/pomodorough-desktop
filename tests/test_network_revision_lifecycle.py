from __future__ import annotations

import unittest

from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWidgets import QApplication

from pomodorough.network import CloudService


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)


class _Reply:
    def __init__(self, status: int) -> None:
        self.status = status
        self.readyRead = _Signal()
        self.finished = _Signal()
        self.deleted = False

    def attribute(self, attribute):
        if attribute == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return self.status
        return None

    def rawHeader(self, _name: str) -> bytes:
        return b"text/event-stream"

    def readAll(self) -> bytes:
        return b""

    def abort(self) -> None:
        return

    def deleteLater(self) -> None:
        self.deleted = True


class _Network:
    def __init__(self, reply: _Reply) -> None:
        self.reply = reply

    def get(self, _request: QNetworkRequest) -> _Reply:
        return self.reply


class RevisionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_unauthorized_stream_invalidates_only_access_credential(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        cloud.access_token = "expired-access"
        cloud.refresh_token = "retained-refresh"
        reply = _Reply(401)
        cloud._network = _Network(reply)
        stale = []
        cloud.authorization_stale.connect(lambda: stale.append(True))

        cloud.start_revision_stream()
        reply.finished.callbacks[0]()

        self.assertEqual(stale, [True])
        self.assertIsNone(cloud.access_token)
        self.assertEqual(cloud.refresh_token, "retained-refresh")
        self.assertTrue(cloud.authenticated)
        self.assertFalse(cloud._revision_reconnect.isActive())
        self.assertTrue(reply.deleted)


if __name__ == "__main__":
    unittest.main()
