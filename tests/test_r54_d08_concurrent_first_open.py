"""R54-D08: concurrent first opens of one new database file.

Switching a new SQLite file to WAL takes a read lock and then asks for the
write lock. SQLite skips the busy handler for that upgrade, so before the fix
a second process or thread opening the same new file failed at once with
"database is locked" while the first open held the write lock.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import time
import traceback
import unittest
from contextlib import closing
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread, Timer
from unittest.mock import patch

import pomodorough.storage as storage
from pomodorough.cli import main
from pomodorough.storage import Store

# Each pair opens like the CLI does, without a SharedCore, so the two opens
# overlap; before the fix about one pair in four failed.
RACE_PAIRS = 100
JOIN_SECONDS = 30
LOCK_HOLD_SECONDS = 1.0
SHORT_WAIT_SECONDS = 0.2
# The outlasting lock ends after the short WAL wait but before the 5 s busy
# timeout of sqlite3.connect. A fix that ignores the lock once the WAL wait
# ends, or retries without a bound, then opens the store instead of raising.
OUTLASTING_LOCK_SECONDS = 3.0
WAL_STATEMENT = "PRAGMA journal_mode=WAL"
PRODUCTION_WAIT_SECONDS = 5.0
PRODUCTION_RETRY_SECONDS = 0.01
BUSY_ATTEMPT_LIMIT = 2000


class WriteLockHolder:
    """Holds the write lock on a new database file, as a first open does."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self._timer: Timer | None = None

    def release_after(self, seconds: float) -> None:
        self._timer = Timer(seconds, self.release)
        self._timer.start()

    def release(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join()
        self.release()
        self.connection.close()


def journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]


def connect_with_failing_wal(error_code: int, attempts: list[float]):
    real_connect = sqlite3.connect

    class FailingWalConnection(sqlite3.Connection):
        def execute(self, sql, *parameters):
            if sql != WAL_STATEMENT:
                return super().execute(sql, *parameters)
            attempts.append(time.monotonic())
            error = sqlite3.OperationalError("injected WAL setup failure")
            error.sqlite_errorcode = error_code
            raise error

    def connect(database, *args, **kwargs):
        return real_connect(database, *args, factory=FailingWalConnection, **kwargs)

    return connect


def connect_with_busy_wal(error_code: int, failures: int, attempts: list[int]):
    """Fail the first WAL statements, then allow normal database setup."""
    real_connect = sqlite3.connect

    class BusyWalConnection(sqlite3.Connection):
        def execute(self, sql, *parameters):
            if sql == WAL_STATEMENT:
                attempts.append(len(attempts) + 1)
                if len(attempts) <= failures:
                    error = sqlite3.OperationalError("injected busy WAL setup")
                    error.sqlite_errorcode = error_code
                    raise error
            return super().execute(sql, *parameters)

    def connect(database, *args, **kwargs):
        return real_connect(database, *args, factory=BusyWalConnection, **kwargs)

    return connect


class SteppedClock:
    """Move the storage clock only when the retry loop sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class ConcurrentFirstOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def hold_write_lock(self, path: Path) -> WriteLockHolder:
        holder = WriteLockHolder(path)
        self.addCleanup(holder.close)
        return holder

    def race_first_opens(self, path: Path) -> list[str]:
        barrier = Barrier(3, timeout=JOIN_SECONDS)
        errors: Queue[str] = Queue()

        def open_store() -> None:
            store = None
            try:
                barrier.wait()
                store = Store(path)
            except BaseException:
                errors.put(traceback.format_exc())
            finally:
                if store is not None:
                    store.close()

        workers = [Thread(target=open_store, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(JOIN_SECONDS)
            self.assertFalse(worker.is_alive(), "Store open exceeded the join timeout")
        return list(errors.queue)

    def test_two_concurrent_first_opens_of_one_new_path_both_succeed(self) -> None:
        for pair in range(RACE_PAIRS):
            path = self.root / f"pair-{pair}" / "state.sqlite3"
            errors = self.race_first_opens(path)
            self.assertEqual(errors, [], f"pair {pair} of {RACE_PAIRS} failed")
            self.assertEqual(journal_mode(path), "wal")

    def test_first_open_waits_for_a_write_lock_released_within_the_wait(self) -> None:
        path = self.root / "state.sqlite3"
        holder = self.hold_write_lock(path)
        holder.release_after(LOCK_HOLD_SECONDS)

        started = time.monotonic()
        store = Store(path)
        elapsed = time.monotonic() - started
        store.close()

        self.assertGreaterEqual(elapsed, LOCK_HOLD_SECONDS * 0.9)
        self.assertEqual(journal_mode(path), "wal")

    def test_write_lock_that_outlasts_the_wait_still_raises_database_locked(self) -> None:
        path = self.root / "state.sqlite3"
        holder = self.hold_write_lock(path)
        holder.release_after(OUTLASTING_LOCK_SECONDS)

        started = time.monotonic()
        with (
            patch.object(storage, "_WAL_SETUP_WAIT_SECONDS", SHORT_WAIT_SECONDS),
            self.assertRaises(sqlite3.OperationalError) as raised,
        ):
            Store(path)
        elapsed = time.monotonic() - started

        self.assertEqual(str(raised.exception), "database is locked")
        self.assertEqual(raised.exception.sqlite_errorcode & 0xFF, sqlite3.SQLITE_BUSY)
        self.assertGreaterEqual(elapsed, SHORT_WAIT_SECONDS)
        self.assertLess(elapsed, OUTLASTING_LOCK_SECONDS)

    def test_cli_reports_a_lock_that_outlasts_the_wait_as_storage_error(self) -> None:
        path = self.root / "state.sqlite3"
        holder = self.hold_write_lock(path)
        holder.release_after(OUTLASTING_LOCK_SECONDS)
        stdout, stderr = io.StringIO(), io.StringIO()

        with patch.object(storage, "_WAL_SETUP_WAIT_SECONDS", SHORT_WAIT_SECONDS):
            result = main(
                ("--data", str(path), "status", "--json"),
                stdout=stdout,
                stderr=stderr,
                locale="en",
            )

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "version": 1,
                "code": "storage_error",
                "type": "StorageError",
                "message": "database is locked",
            },
        )

    def test_non_busy_error_during_wal_setup_raises_without_retry(self) -> None:
        path = self.root / "state.sqlite3"
        attempts: list[float] = []
        connect = connect_with_failing_wal(sqlite3.SQLITE_IOERR, attempts)

        with (
            patch.object(storage.sqlite3, "connect", connect),
            self.assertRaises(sqlite3.OperationalError) as raised,
        ):
            Store(path)

        self.assertEqual(str(raised.exception), "injected WAL setup failure")
        self.assertEqual(len(attempts), 1)

    def test_failed_open_closes_its_connection(self) -> None:
        """Windows keeps a file locked while any connection to it stays open."""
        path = self.root / "state.sqlite3"
        opened: list[sqlite3.Connection] = []
        failing_connect = connect_with_failing_wal(sqlite3.SQLITE_IOERR, [])

        def connect(database, *args, **kwargs):
            opened.append(failing_connect(database, *args, **kwargs))
            return opened[-1]

        with (
            patch.object(storage.sqlite3, "connect", connect),
            self.assertRaises(sqlite3.OperationalError),
        ):
            Store(path)

        self.assertEqual(len(opened), 1)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
            opened[0].execute("SELECT 1")
        path.unlink()

    def test_extended_busy_code_is_retried_after_the_retry_spacing(self):
        path = self.root / "state.sqlite3"
        attempts = []
        clock = SteppedClock()
        connect = connect_with_busy_wal(sqlite3.SQLITE_BUSY_RECOVERY, 1, attempts)
        with (
            patch.object(storage.sqlite3, "connect", connect),
            patch.object(storage, "time", clock),
        ):
            store = Store(path)
        store.close()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(clock.sleeps, [PRODUCTION_RETRY_SECONDS])
        self.assertEqual(journal_mode(path), "wal")

    def test_busy_wal_setup_raises_after_the_production_wait(self):
        path = self.root / "state.sqlite3"
        attempts = []
        clock = SteppedClock()
        started = clock.now
        connect = connect_with_busy_wal(sqlite3.SQLITE_BUSY, BUSY_ATTEMPT_LIMIT, attempts)
        with (
            patch.object(storage.sqlite3, "connect", connect),
            patch.object(storage, "time", clock),
            self.assertRaises(sqlite3.OperationalError) as raised,
        ):
            Store(path)
        waited = clock.now - started
        self.assertEqual(str(raised.exception), "injected busy WAL setup")
        self.assertGreaterEqual(waited, PRODUCTION_WAIT_SECONDS)
        self.assertLess(waited, PRODUCTION_WAIT_SECONDS + 2 * PRODUCTION_RETRY_SECONDS)
        self.assertEqual(set(clock.sleeps), {PRODUCTION_RETRY_SECONDS})
        self.assertEqual(len(clock.sleeps), len(attempts) - 1)


if __name__ == "__main__":
    unittest.main()
