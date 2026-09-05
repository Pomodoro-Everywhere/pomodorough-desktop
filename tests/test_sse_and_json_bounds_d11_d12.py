from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject

from pomodorough import network_account
from pomodorough.network import (
    ApiError,
    TokenStore,
    _FALLBACK_DOCUMENT_LIMIT,
    _OAUTH_CREDENTIALS_LIMIT,
    _parse_oauth_credentials,
)
from pomodorough.network_revision import (
    MAX_SSE_BUFFER_BYTES,
    RevisionEventOverflowError,
    RevisionEventParser,
    RevisionStream,
)
from pomodorough.secure_store import (
    PlatformSecretStore,
    SecureStoreError,
    _WINDOWS_BLOB_LIMIT,
)


class _FakeStreamReply:
    def __init__(self, body: bytes = b"") -> None:
        self.body = body
        self.aborted = False
        self.deleted = False

    def readAll(self) -> bytes:
        body, self.body = self.body, b""
        return body

    def attribute(self, _attribute):
        return 200

    def rawHeader(self, name: str) -> bytes:
        return b"text/event-stream" if name == "Content-Type" else b""

    def abort(self) -> None:
        self.aborted = True

    def deleteLater(self) -> None:
        self.deleted = True


def _stream_with_reply(reply, parser=None):
    app = QApplication.instance() or QApplication([])
    parent = QObject()
    parent._held = app
    stream = RevisionStream(parent, "https://example.test", lambda: None, lambda upper: 0)
    stream.state.reply = reply
    stream.state.parser = parser or RevisionEventParser()
    return stream


class SSEParserBoundsTests(unittest.TestCase):
    def test_buffer_without_newline_overflows_and_resets(self) -> None:
        parser = RevisionEventParser(16, 16, 16)
        with self.assertRaises(RevisionEventOverflowError):
            parser.feed(b"x" * 17)
        self.assertEqual(parser.buffer, b"")
        self.assertEqual(parser.data_lines, [])
        self.assertEqual(parser.feed(b"data: 7\n\n"), [7])

    def test_single_line_overflows_and_resets(self) -> None:
        parser = RevisionEventParser(1024, 16, 1024)
        with self.assertRaises(RevisionEventOverflowError):
            parser.feed(b"data: " + b"y" * 32 + b"\n")
        self.assertEqual(parser.buffer, b"")
        self.assertEqual(parser.feed(b"data: 8\n\n"), [8])

    def test_event_payload_overflows_and_resets(self) -> None:
        parser = RevisionEventParser(1024, 1024, 16)
        with self.assertRaises(RevisionEventOverflowError):
            parser.feed(b"data: 12345678901234567\n\n")
        self.assertEqual(parser.feed(b"data: 9\n\n"), [9])

    def test_exact_buffer_limit_accepted(self) -> None:
        parser = RevisionEventParser(16, 1024, 1024)
        self.assertEqual(parser.feed(b"x" * 15 + b"\n"), [])
        parser = RevisionEventParser(16, 1024, 1024)
        with self.assertRaises(RevisionEventOverflowError):
            parser.feed(b"x" * 16 + b"\n")

    def test_default_limits_reject_huge_line(self) -> None:
        parser = RevisionEventParser()
        with self.assertRaises(RevisionEventOverflowError):
            parser.feed(b"x" * (MAX_SSE_BUFFER_BYTES + 1))
        self.assertEqual(parser.feed(b"data: 11\n\n"), [11])


class RevisionStreamOverflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_read_drops_buffer_and_aborts_on_overflow(self) -> None:
        reply = _FakeStreamReply(b"x" * 32)
        stream = _stream_with_reply(reply, RevisionEventParser(16, 1024, 1024))
        self.assertEqual(stream.read(reply), ())
        self.assertTrue(reply.aborted)
        self.assertEqual(stream.state.parser.buffer, b"")
        reply.body = b"data: 42\n\n"
        self.assertEqual(stream.read(reply), (42,))

    def test_line_overflow_aborts_and_recovers(self) -> None:
        reply = _FakeStreamReply(b"data: " + b"z" * 64 + b"\n")
        stream = _stream_with_reply(reply, RevisionEventParser(1024, 16, 1024))
        self.assertEqual(stream.read(reply), ())
        self.assertTrue(reply.aborted)
        reply.body = b"data: 43\n\n"
        self.assertEqual(stream.read(reply), (43,))


class OAuthCredentialsBoundsTests(unittest.TestCase):
    def test_oversized_oauth_file_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "oauth.json"
            source.write_bytes(b"x" * (_OAUTH_CREDENTIALS_LIMIT + 1))
            with self.assertRaises(ApiError):
                _parse_oauth_credentials(source)

    def test_exact_limit_oauth_file_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "oauth.json"
            padding = _OAUTH_CREDENTIALS_LIMIT - 100
            document = {"client_id": "id-" + "x" * padding}
            encoded = json.dumps(document).encode("utf-8")
            self.assertLessEqual(len(encoded), _OAUTH_CREDENTIALS_LIMIT)
            source.write_bytes(encoded)
            credentials = _parse_oauth_credentials(source)
            self.assertEqual(credentials["client_id"], document["client_id"])

    def test_small_oauth_file_still_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "oauth.json"
            source.write_text(json.dumps({"client_id": "abc"}), encoding="utf-8")
            self.assertEqual(_parse_oauth_credentials(source)["client_id"], "abc")


class FallbackTombstoneBoundsTests(unittest.TestCase):
    def _store(self, root: Path) -> TokenStore:
        return TokenStore("device-1", secret_store=None, fallback_path=root / "s.json")

    def test_oversized_fallback_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "s.json").write_bytes(b"x" * (_FALLBACK_DOCUMENT_LIMIT + 1))
            with self.assertRaises(SecureStoreError):
                self._store(root)._load_fallback()

    def test_exact_limit_fallback_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = b'{"signedOut":false,"pad":"'
            suffix = b'"}'
            pad = _FALLBACK_DOCUMENT_LIMIT - len(prefix) - len(suffix)
            (root / "s.json").write_bytes(prefix + b"p" * pad + suffix)
            document = self._store(root)._load_fallback()
            self.assertIsNotNone(document)


class DeletionCleanupBoundsTests(unittest.TestCase):
    def test_blocks_authentication_rejects_oversized(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cleanup.json"
            path.write_bytes(b"x" * (network_account._DELETION_CLEANUP_LIMIT + 1))
            with self.assertRaises(SecureStoreError):
                network_account._account_deletion_cleanup_blocks_authentication(path)

    def test_locked_read_rejects_oversized(self) -> None:
        from pomodorough.network_account import AccountLifecycle

        with TemporaryDirectory() as directory:
            path = Path(directory) / "cleanup.json"
            path.write_bytes(b"x" * (network_account._DELETION_CLEANUP_LIMIT + 1))
            lifecycle = AccountLifecycle.__new__(AccountLifecycle)
            with self.assertRaises(SecureStoreError):
                lifecycle._read_deletion_cleanup_locked(path)


class WindowsBlobBoundsTests(unittest.TestCase):
    def _store(self, root: Path) -> PlatformSecretStore:
        return PlatformSecretStore(root)

    def test_oversized_blob_rejected_before_unprotect(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            blob = root / "blob"
            blob.write_bytes(b"x" * (_WINDOWS_BLOB_LIMIT + 1))
            with (
                patch.object(store, "_windows_path", return_value=blob),
                patch.object(PlatformSecretStore, "_windows_unprotect") as unprotect,
                patch.object(PlatformSecretStore, "_validate_key", return_value=None),
                patch("pomodorough.secure_store.os") as mock_os,
                self.assertRaises(SecureStoreError),
            ):
                mock_os.name = "nt"
                store.load("k")
            unprotect.assert_not_called()

    def test_exact_limit_blob_reaches_unprotect(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root)
            blob = root / "blob"
            blob.write_bytes(b"x" * _WINDOWS_BLOB_LIMIT)
            with (
                patch.object(store, "_windows_path", return_value=blob),
                patch.object(
                    PlatformSecretStore, "_windows_unprotect", return_value=b"ok"
                ) as unprotect,
                patch.object(PlatformSecretStore, "_validate_key", return_value=None),
                patch("pomodorough.secure_store.os") as mock_os,
            ):
                mock_os.name = "nt"
                self.assertEqual(store.load("k"), b"ok")
            unprotect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
