"""R49-D03 residual: journal-write boundary and recovery pending flag."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pomodorough.network import TokenStore
from pomodorough.secure_store import TokenCommitPendingError


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _tokens(name: str) -> dict[str, str]:
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def _make_store(tmp: Path, secret: _MemorySecretStore) -> TokenStore:
    store = TokenStore(
        "d03-device", secret_store=secret, fallback_path=tmp / "session.json"
    )
    store.bind_api("https://example.test")
    return store


class JournalBoundaryTests(unittest.TestCase):
    def test_journal_write_failure_is_pre_commit_old_authoritative(self) -> None:
        with TemporaryDirectory() as name:
            tmp = Path(name)
            secret = _MemorySecretStore()
            store = _make_store(tmp, secret)
            from pomodorough.network_session import (
                AuthenticatedSession,
                SessionState,
            )
            from datetime import UTC, datetime

            session = AuthenticatedSession(
                "https://example.test",
                SessionState(),
                store,
                lambda *a, **k: {},
                lambda k, **v: k,
                lambda: datetime.now(UTC),
                lambda: 0.0,
                lambda: 0,
            )
            session.accept_tokens(_tokens("old"))
            old_secret = secret.values.get("oauth:d03-device")

            def failing_write(*a: object, **k: object) -> None:
                raise OSError("journal write denied")

            with patch.object(
                TokenStore, "_write_credential_commit_locked", side_effect=failing_write
            ):
                with self.assertRaises(OSError) as ctx:
                    session.accept_tokens(_tokens("new"))
                self.assertNotIsInstance(ctx.exception, TokenCommitPendingError)
            self.assertEqual(secret.values.get("oauth:d03-device"), old_secret)
            self.assertEqual(session.state.refresh_token, "old-refresh")
            self.assertFalse(store._credential_commit_path().exists())
            session.accept_tokens(_tokens("new"))
            self.assertEqual(
                json.loads(secret.values["oauth:d03-device"].decode())["refreshToken"],
                "new-refresh",
            )

    @unittest.skipIf(os.name == "nt", "POSIX atomic replace semantics require Unix")
    def test_recovery_replay_failure_reports_during_recovery(self) -> None:
        with TemporaryDirectory() as name:
            tmp = Path(name)
            secret = _MemorySecretStore()
            store = _make_store(tmp, secret)
            from pomodorough.network_session import (
                AuthenticatedSession,
                SessionState,
            )
            from datetime import UTC, datetime

            session = AuthenticatedSession(
                "https://example.test",
                SessionState(),
                store,
                lambda *a, **k: {},
                lambda k, **v: k,
                lambda: datetime.now(UTC),
                lambda: 0.0,
                lambda: 0,
            )
            session.accept_tokens(_tokens("old"))
            real_replace = os.replace
            identity_path = store._account_deletion_identity_path()

            def replace(src: Path, dst: Path, *a: object, **k: object):
                if Path(dst) == identity_path:
                    raise PermissionError("identity replace denied")
                return real_replace(src, dst, *a, **k)

            with patch("pomodorough.network.os.replace", side_effect=replace):
                with self.assertRaises(TokenCommitPendingError) as pending:
                    session.accept_tokens(_tokens("new"))
                self.assertFalse(pending.exception.during_recovery)
            self.assertTrue(store._credential_commit_path().exists())
            with patch("pomodorough.network.os.replace", side_effect=replace):
                with self.assertRaises(TokenCommitPendingError) as recovery:
                    store.load()
                self.assertTrue(recovery.exception.during_recovery)


if __name__ == "__main__":
    unittest.main()
