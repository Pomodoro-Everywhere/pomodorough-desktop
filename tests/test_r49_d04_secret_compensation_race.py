"""R49-D04: secret compensation must not overwrite later peer/room commit."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pomodorough import secure_store
from pomodorough.secure_store import PlatformSecretStore, SecretMutationJournal
from pomodorough.storage import Store

OLD_TICKET = "old-ticket"
NEXT_TICKET = "ticket-after"
SECOND_TICKET = "second-committed-ticket"


class SharedVault(PlatformSecretStore):
    def __init__(self, root: Path, values: dict[str, bytes]) -> None:
        super().__init__(root)
        self.values = values

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, secret: bytes) -> None:
        self.values[key] = secret

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@contextmanager
def thread_safe_sqlite():
    original = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs["check_same_thread"] = False
        return original(*args, **kwargs)

    with patch.object(sqlite3, "connect", side_effect=connect):
        yield


@contextmanager
def isolated_locks(tmp: Path):
    with patch.object(secure_store, "user_config_path", return_value=tmp):
        yield


def make_pair(tmp: Path):
    values: dict[str, bytes] = {}
    vault = SharedVault(tmp / "vault", values)
    path = tmp / "state.sqlite3"
    first = Store(path, iroh_secret_store=vault)
    second = Store(path, iroh_secret_store=vault)
    return first, second, vault


@contextmanager
def denied_first_commit(first: Store):
    storage = first._replication_storage
    transactions = storage._transactions
    rooms = storage._rooms
    original_tx = transactions._dependencies
    original_room = rooms._dependencies

    @contextmanager
    def failing():
        first.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            first.connection.rollback()
        raise OSError("R49-D04 denied COMMIT")

    transactions._dependencies = replace(
        original_tx, immediate_transaction=failing
    )
    rooms._dependencies = replace(original_room, immediate_transaction=failing)
    try:
        yield
    finally:
        transactions._dependencies = original_tx
        rooms._dependencies = original_room


@contextmanager
def paused_compensation():
    reached = threading.Event()
    release = threading.Event()
    original = SecretMutationJournal.__exit__
    called = False

    def pausing(self, error_type, error, traceback):
        nonlocal called
        if error_type is not None and not called:
            called = True
            reached.set()
            assert release.wait(10), "compensation release missing"
        return original(self, error_type, error, traceback)

    with patch.object(SecretMutationJournal, "__exit__", pausing):
        yield reached, release


def run_thread(operation, box: list) -> threading.Thread:
    def target() -> None:
        try:
            operation()
        except BaseException as error:
            box.append(error)
        else:
            box.append(None)

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def assert_connections_idle(*stores: Store) -> None:
    for store in stores:
        assert not store.connection.in_transaction
        store.connection.execute("SELECT 1").fetchone()


class PeerCompensationRaceTests(unittest.TestCase):
    def test_denied_commit_preserves_later_peer_update(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with thread_safe_sqlite(), isolated_locks(tmp / "locks"):
                self._peer_race(tmp)

    def _peer_race(self, tmp: Path) -> None:
        first, second, vault = make_pair(tmp)
        try:
            room = first.create_iroh_room(bytes(range(32)))
            first.upsert_iroh_peer(room, "endpoint-1", OLD_TICKET, "old", None)
            key = first._peer_ticket_key(room, "endpoint-1")
            self.assertEqual(vault.load(key), b"old-ticket")
            with denied_first_commit(first), paused_compensation() as events:
                reached, release = events
                first_box: list = []
                first_thread = run_thread(
                    lambda: first.upsert_iroh_peer(
                        room, "endpoint-1", NEXT_TICKET, "new", None
                    ),
                    first_box,
                )
                self.assertTrue(reached.wait(10))
                second_box: list = []
                second_thread = run_thread(
                    lambda: second.upsert_iroh_peer(
                        room, "endpoint-1", SECOND_TICKET, "second-device", None
                    ),
                    second_box,
                )
                second_thread.join(0.3)
                self.assertTrue(first_thread.is_alive())
                release.set()
                first_thread.join(10)
                second_thread.join(10)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())
                self.assertIsInstance(first_box[0], OSError)
                self.assertIsNone(second_box[0])
                peers = second.iroh_peers(room)
                self.assertEqual(peers[0]["deviceId"], "second-device")
                self.assertEqual(peers[0]["endpointTicket"], SECOND_TICKET)
                self.assertEqual(vault.load(key), SECOND_TICKET.encode())
                self.assertNotEqual(vault.load(key), b"old-ticket")
                self.assertNotEqual(vault.load(key), NEXT_TICKET.encode())
                assert_connections_idle(first, second)
        finally:
            first.close()
            second.close()

    def test_denied_create_preserves_later_room_secret(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with thread_safe_sqlite(), isolated_locks(tmp / "locks"):
                self._room_race(tmp)

    def _room_race(self, tmp: Path) -> None:
        first, second, vault = make_pair(tmp)
        try:
            secret = bytes([7]) * 32
            with denied_first_commit(first), paused_compensation() as events:
                reached, release = events
                first_box: list = []
                first_thread = run_thread(
                    lambda: first.create_iroh_room(secret), first_box
                )
                self.assertTrue(reached.wait(10))
                second_box: list = []
                second_thread = run_thread(
                    lambda: second.create_iroh_room(secret), second_box
                )
                second_thread.join(0.3)
                self.assertTrue(first_thread.is_alive())
                release.set()
                first_thread.join(10)
                second_thread.join(10)
                self.assertIsInstance(first_box[0], OSError)
                self.assertIsNone(second_box[0])
                room_id = second.active_iroh_room_id
                self.assertIsNotNone(room_id)
                assert room_id is not None
                self.assertEqual(vault.load(second._room_secret_key(room_id)), secret)
                self.assertIsNotNone(second.iroh_room(room_id))
                assert_connections_idle(first, second)
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
