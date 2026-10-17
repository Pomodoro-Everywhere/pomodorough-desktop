"""Disk-backed credentials and independent expectations for journal regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pomodorough import network
from pomodorough.network import TokenStore
from pomodorough.network_session import AuthenticatedSession, SessionState

API = "https://r49-d03.example.test"
DEVICE = "journal-boundary-device"
KEY = f"oauth:{DEVICE}"
OLD = {
    "accessToken": "journal-old-access",
    "accessTokenExpiresAt": "2098-01-02T03:04:05Z",
    "refreshToken": "journal-old-refresh",
    "refreshTokenExpiresAt": "2098-02-03T04:05:06Z",
}
NEW = {
    "accessToken": "journal-new-access",
    "accessTokenExpiresAt": "2099-05-06T07:08:09Z",
    "refreshToken": "journal-new-refresh",
    "refreshTokenExpiresAt": "2099-06-07T08:09:10Z",
}


def encode(document, *, sorted_keys=False):
    return json.dumps(document, separators=(",", ":"), sort_keys=sorted_keys).encode()


def stored(tokens):
    return {
        "refreshToken": tokens["refreshToken"],
        "refreshTokenExpiresAt": tokens["refreshTokenExpiresAt"],
        "apiBase": API,
    }


def identity(tokens):
    return {
        "accessTokenHash": hashlib.sha256(tokens["accessToken"].encode()).hexdigest(),
        "apiBase": API,
        "refreshTokenHash": hashlib.sha256(tokens["refreshToken"].encode()).hexdigest(),
        "version": 1,
    }


JOURNAL = encode({"identity": identity(NEW), "stored": stored(NEW), "version": 1},
                 sorted_keys=True)
REPLAYED_PROVIDER = encode(json.loads(JOURNAL)["stored"])
OLD_FALLBACK = json.dumps(stored(OLD), indent=2).encode() + b"\n"


def private_write(path, raw):
    with path.open("wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileProvider:
    """Persist actual provider bytes across independent Python processes."""

    def __init__(self, root):
        self.path = root / "provider.bin"
        self.calls = []
        self.failure = None

    def load(self, key):
        assert key == KEY
        return self.path.read_bytes() if self.path.exists() else None

    def save(self, key, raw):
        assert key == KEY
        self.calls.append((key, raw))
        if self.failure is not None:
            raise self.failure
        private_write(self.path, raw)

    def delete(self, key):
        assert key == KEY
        self.path.unlink(missing_ok=True)


def make_store(root):
    provider = FileProvider(root)
    store = TokenStore(DEVICE, secret_store=provider, fallback_path=root / "session.json")
    store.bind_api(API)
    return store


def memory(session):
    # Include every state field, including future fields, while preserving lock identity.
    return {key: value if key == "lock" else copy.deepcopy(value)
            for key, value in vars(session.state).items()}


def expected_memory(before, tokens):
    return dict(before, access_token=tokens["accessToken"],
                refresh_token=tokens["refreshToken"],
                access_expires_at=datetime.fromisoformat(
                    tokens["accessTokenExpiresAt"]))


def paths(root):
    suffix = hashlib.sha256(KEY.encode()).hexdigest()[:16]
    return {
        "provider": root / "provider.bin",
        "identity": root / f".session.json.account-identity-{suffix}",
        "journal": root / f".session.json.credential-commit-{suffix}",
        "fallback": root / "session.json",
    }


def disk(root):
    return {name: path.read_bytes() if path.exists() else None
            for name, path in paths(root).items()}


def expected_disk(tokens, fallback, *, journal=None, replayed=False):
    return {
        "provider": REPLAYED_PROVIDER if replayed else encode(stored(tokens)),
        "identity": encode(identity(tokens), sorted_keys=True),
        "journal": journal,
        "fallback": OLD_FALLBACK if fallback else None,
    }


class CredentialCase:
    def __init__(self, root, fallback):
        self.root = root
        self.store = make_store(root)
        self.provider = self.store.secret_store
        state = SessionState(account_generation=49, authenticated=True, busy=True,
                             deleting_account=True, shutting_down=True,
                             sync_queued={"unchanged": ["marker", 3]})
        self.session = AuthenticatedSession(
            API, state, self.store, lambda *args, **kwargs: {},
            lambda key, **kwargs: key, lambda: datetime(2026, 10, 16, tzinfo=UTC),
            lambda: 0.0, lambda: 0,
        )
        self.session.accept_tokens(OLD)
        if fallback:
            private_write(self.store.fallback_path, OLD_FALLBACK)
        assert disk(root) == expected_disk(OLD, fallback)
        assert self.store._credential_commit_path() == paths(root)["journal"]
        assert self.store._account_deletion_identity_path() == paths(root)["identity"]
        self.before = memory(self.session)
        self.state, self.lock, self.queue = state, state.lock, state.sync_queued
        self.provider.calls.clear()

    def assert_memory(self, tokens):
        assert self.session.state is self.state
        assert self.state.lock is self.lock
        assert self.state.sync_queued is self.queue
        actual, expected = memory(self.session), expected_memory(self.before, tokens)
        assert actual == expected, {"session": actual, "expected_session": expected}


def child_load(root):
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), str(root)],
        capture_output=True, text=True, check=True, timeout=30,
        env={**os.environ, "POMODOROUGH_SENTRY_DISABLE": "1"},
    )
    response = json.loads(result.stdout)
    assert response["pid"] != os.getpid()
    assert response["source"] == str(Path(network.__file__).resolve())
    assert response["python"] == sys.executable
    response["disk"] = {key: bytes.fromhex(value) if value is not None else None
                        for key, value in response["disk"].items()}
    return response


def recover(case, *, process_first):
    observations = []
    if process_first:
        observations.append(child_load(case.root))
    observations.append({"loaded": case.store.load(), "disk": disk(case.root)})
    fresh = make_store(case.root)
    observations.append({"loaded": fresh.load(), "disk": disk(case.root)})
    observations.append(child_load(case.root))
    return observations


def assert_recovery(case, observations, tokens, fallback):
    final = expected_disk(tokens, fallback, replayed=tokens == NEW)
    for observation in observations:
        assert observation["loaded"] == stored(tokens), observation
        assert observation["disk"] == final, observation
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600
               for path in paths(case.root).values() if path.exists())


def exception_chain(error):
    found, pending = [], [error]
    while pending:
        current = pending.pop()
        if any(current is previous for previous in found):
            continue
        found.append(current)
        pending.extend(item for item in (current.__cause__, current.__context__)
                       if item is not None)
    return found


if __name__ == "__main__":
    root = Path(sys.argv[1])
    result = make_store(root).load()
    print(json.dumps({
        "pid": os.getpid(), "python": sys.executable,
        "source": str(Path(network.__file__).resolve()), "loaded": result,
        "disk": {key: value.hex() if value is not None else None
                 for key, value in disk(root).items()},
    }))
