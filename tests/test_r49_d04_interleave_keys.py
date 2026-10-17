"""R49-D04 residual: later-commit guard and multi-key serialization."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pomodorough import secure_store
from pomodorough.secure_store import PlatformSecretStore, SecretMutationJournal
from pomodorough.storage import Store


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


class InterleaveKeyTests(unittest.TestCase):
    def test_nonjournal_later_write_survives_compensation_guard(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with isolated_locks(tmp / "locks"):
                values: dict[str, bytes] = {}
                vault = SharedVault(tmp / "vault", values)
                journal = SecretMutationJournal(vault)
                with self.assertRaises(OSError):
                    with journal:
                        journal.save("k-guard", b"first-ticket")
                        # Later commit outside the journal, after rollback would
                        # release SQLite ownership in production.
                        vault.save("k-guard", b"later-committed-ticket")
                        raise OSError("R49-D04 denied commit")
                self.assertEqual(vault.load("k-guard"), b"later-committed-ticket")

    def test_opposite_order_room_peer_journals_do_not_deadlock(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            with thread_safe_sqlite(), isolated_locks(tmp / "locks"):
                values: dict[str, bytes] = {}
                vault = SharedVault(tmp / "vault", values)
                path = tmp / "state.sqlite3"
                first = Store(path, iroh_secret_store=vault)
                second = Store(path, iroh_secret_store=vault)
                try:
                    first_errors: list = []
                    second_errors: list = []

                    def first_work() -> None:
                        try:
                            with SecretMutationJournal(vault):
                                vault.save("k-one", b"first-one")
                                vault.save("k-two", b"first-two")
                                raise OSError("first denied")
                        except BaseException as error:  # noqa: BLE001
                            first_errors.append(error)
                        else:
                            first_errors.append(None)

                    def second_work() -> None:
                        try:
                            with SecretMutationJournal(vault):
                                vault.save("k-two", b"second-two")
                                vault.save("k-one", b"second-one")
                        except BaseException as error:  # noqa: BLE001
                            second_errors.append(error)
                        else:
                            second_errors.append(None)

                    first_thread = threading.Thread(target=first_work)
                    second_thread = threading.Thread(target=second_work)
                    first_thread.start()
                    second_thread.start()
                    first_thread.join(15)
                    second_thread.join(15)
                    self.assertFalse(first_thread.is_alive())
                    self.assertFalse(second_thread.is_alive())
                    self.assertIsInstance(first_errors[0], OSError)
                    self.assertIsNone(second_errors[0])
                    self.assertEqual(vault.load("k-one"), b"second-one")
                    self.assertEqual(vault.load("k-two"), b"second-two")
                    self.assertFalse(first.connection.in_transaction)
                    self.assertFalse(second.connection.in_transaction)
                finally:
                    first.close()
                    second.close()


if __name__ == "__main__":
    unittest.main()
