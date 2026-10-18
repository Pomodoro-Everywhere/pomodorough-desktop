"""R50-D01: uncertain provider save retains the recovery journal.

A provider that writes credentials and then fails reports an unknown
outcome. The commit boundary must quarantine (retain journal, report
pending) instead of discarding the journal. Only proven pre-mutation
rejections may abort. Uses the real Linux PlatformSecretStore adapter
against a synthetic secret-tool executable, plus a fresh child process.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from pomodorough.network import TokenStore
from pomodorough.network_session import AuthenticatedSession, SessionState
from pomodorough.secure_store import (
    PlatformSecretStore,
    SecureStoreError,
    TokenCommitPendingError,
    UncertainSecretStoreError,
    is_infra_secure_store_error,
)
from test_secure_store import linux_secret_store

API = "https://r50-d01.example.test"
DEVICE = "r50-d01-device"
KEY = f"oauth:{DEVICE}"
TOOL = """#!{python}
import json, os, sys


def key_of(args):
    for position, token in enumerate(args):
        if token == "key" and position + 1 < len(args):
            return args[position + 1]
    raise SystemExit(3)


database = os.environ["R50_DB"]
try:
    with open(database) as handle:
        items = json.load(handle)
except FileNotFoundError:
    items = {{}}
operation, args = sys.argv[1], sys.argv[2:]
if operation == "lookup":
    value = items.get(key_of(args))
    if value is None:
        raise SystemExit(1)
    sys.stdout.write(value + "\\n")
elif operation == "store":
    items[key_of(args)] = sys.stdin.read().strip()
    with open(database, "w") as handle:
        handle.write(json.dumps(items))
        handle.flush()
        os.fsync(handle.fileno())
    if os.environ.get("R50_FAIL_AFTER_WRITE") == "1":
        sys.stderr.write("lost acknowledgement after durable write\\n")
        raise SystemExit(2)
elif operation == "clear":
    items.pop(key_of(args), None)
    with open(database, "w") as handle:
        handle.write(json.dumps(items))
        handle.flush()
        os.fsync(handle.fileno())
else:
    raise SystemExit(3)
"""
PROBE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from pomodorough import secure_store\n"
    "from pomodorough.network import TokenStore\n"
    "root = Path(sys.argv[1])\n"
    "secure_store.sys_platform = lambda: 'linux'\n"
    "secure_store.user_config_path = lambda *a, **k: root\n"
    "store = TokenStore(sys.argv[2], fallback_path=root / 'session.json')\n"
    "store.bind_api(sys.argv[3])\n"
    "report = {'journal': store._credential_commit_path().exists()}\n"
    "try:\n"
    "    report['loaded'] = store.load()\n"
    "except Exception as error:\n"
    "    report['error'] = type(error).__name__ + ': ' + str(error)\n"
    "try:\n"
    "    with store.account_deletion_credentials_locked():\n"
    "        identity = store._account_deletion_identity_locked()\n"
    "        report['identity'] = None if identity is None else identity.refresh_token_hash\n"
    "except Exception as error:\n"
    "    report['identity_error'] = type(error).__name__ + ': ' + str(error)\n"
    "print(json.dumps(report))\n"
)


def _tokens(name):
    return {
        "accessToken": f"{name}-access",
        "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
        "refreshToken": f"{name}-refresh",
        "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
    }


def _stored(tokens):
    return {
        "refreshToken": tokens["refreshToken"],
        "refreshTokenExpiresAt": tokens["refreshTokenExpiresAt"],
        "apiBase": API,
    }


def _identity_hash(tokens):
    return hashlib.sha256(tokens["refreshToken"].encode()).hexdigest()


def _install_tool(root):
    binary = root / "bin" / "secret-tool"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(TOOL.format(python=sys.executable))
    binary.chmod(0o700)
    return binary.parent


def _make_store(root):
    store = TokenStore(DEVICE, fallback_path=root / "session.json")
    store.bind_api(API)
    return store


def _make_session(store):
    return AuthenticatedSession(
        API, SessionState(), store, lambda *a, **k: {},
        lambda k, **v: k, lambda: datetime.now(UTC),
        lambda: 0.0, lambda: 0,
    )


def _run_probe(root, environment):
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(root), DEVICE, API],
        capture_output=True, text=True, timeout=60,
        env={**environment, "POMODOROUGH_SENTRY_DISABLE": "1"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assert_converged(store, session):
    assert store.load() == _stored(_tokens("new"))
    assert not store._credential_commit_path().exists()
    with store.account_deletion_credentials_locked():
        identity = store._account_deletion_identity_locked()
        stored = store._account_deletion_candidate_locked()
        assert stored is not None and identity is not None
        assert store._stored_credentials_match_identity(stored, identity)
        assert stored["refreshToken"] == "new-refresh"
    assert session.state.refresh_token == "new-refresh"


@pytest.mark.skipif(os.name == "nt", reason="Synthetic secret-tool requires POSIX")
@pytest.mark.parametrize("fallback", [False, True], ids=["no-fallback", "old-fallback"])
def test_write_then_error_retains_journal_and_converges(tmp_path, monkeypatch, fallback):
    monkeypatch.setenv("R50_DB", str(tmp_path / "provider.json"))
    monkeypatch.setenv(
        "PATH", str(_install_tool(tmp_path)) + os.pathsep + os.environ["PATH"])
    with linux_secret_store(tmp_path):
        store = _make_store(tmp_path)
        session = _make_session(store)
        assert isinstance(store.secret_store, PlatformSecretStore)
        session.accept_tokens(_tokens("old"))
        if fallback:
            store.fallback_path.write_text('{"legacy":true}')
        old_identity = store._account_deletion_identity_path().read_bytes()
        monkeypatch.setenv("R50_FAIL_AFTER_WRITE", "1")
        with pytest.raises(TokenCommitPendingError) as pending:
            session.accept_tokens(_tokens("new"))
        assert pending.value.during_recovery is False
        assert store._credential_commit_path().exists()
        assert json.loads(store.secret_store.load(KEY).decode()) == _stored(_tokens("new"))
        assert session.state.refresh_token == "new-refresh"
        assert store._account_deletion_identity_path().read_bytes() == old_identity
        faulted = _run_probe(tmp_path, dict(os.environ))
        assert faulted["journal"] is True
        assert "TokenCommitPendingError" in faulted.get("error", "")
        monkeypatch.delenv("R50_FAIL_AFTER_WRITE")
        session.accept_tokens(_tokens("new"))
        _assert_converged(store, session)
        healed = _run_probe(tmp_path, dict(os.environ))
        assert healed.get("error") is None and healed["journal"] is False
        assert healed["loaded"] == _stored(_tokens("new"))
        assert healed["identity"] == _identity_hash(_tokens("new"))


def test_write_then_timeout_retains_journal_and_converges(tmp_path):
    fault = {"active": False}

    class _WriteThenTimeoutStore:
        def __init__(self):
            self.values = {}

        def load(self, key):
            return self.values.get(key)

        def save(self, key, value):
            self.values[key] = value
            if fault["active"]:
                cause = subprocess.TimeoutExpired(["secret-tool", "store"], 15)
                raise UncertainSecretStoreError("Platform secure storage timed out") from cause

        def delete(self, key):
            self.values.pop(key, None)

    store = TokenStore(DEVICE, secret_store=_WriteThenTimeoutStore(),
                       fallback_path=tmp_path / "session.json")
    store.bind_api(API)
    session = _make_session(store)
    session.accept_tokens(_tokens("old"))
    fault["active"] = True
    with pytest.raises(TokenCommitPendingError) as pending:
        session.accept_tokens(_tokens("new"))
    assert pending.value.during_recovery is False
    assert store._credential_commit_path().exists()
    assert json.loads(store.secret_store.values[KEY].decode()) == _stored(_tokens("new"))
    assert session.state.refresh_token == "new-refresh"
    fault["active"] = False
    session.accept_tokens(_tokens("new"))
    _assert_converged(store, session)


@pytest.mark.skipif(os.name == "nt", reason="Synthetic secret-tool requires POSIX")
def test_clean_pre_mutation_tool_rejection_still_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("R50_DB", str(tmp_path / "provider.json"))
    monkeypatch.setenv(
        "PATH", str(_install_tool(tmp_path)) + os.pathsep + os.environ["PATH"])
    with linux_secret_store(tmp_path):
        store = _make_store(tmp_path)
        session = _make_session(store)
        session.accept_tokens(_tokens("old"))
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(SecureStoreError) as failure:
            session.accept_tokens(_tokens("new"))
        assert not isinstance(failure.value, TokenCommitPendingError)
        assert "secret-tool was not found" in str(failure.value)
        assert not store._credential_commit_path().exists()
        monkeypatch.setenv(
            "PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
        assert store.load() == _stored(_tokens("old"))
        assert session.state.refresh_token == "old-refresh"
        session.accept_tokens(_tokens("new"))
        assert store.load() == _stored(_tokens("new"))
        assert not store._credential_commit_path().exists()


def test_plain_oserror_reject_still_aborts(tmp_path):
    store = _make_store(tmp_path)
    session = _make_session(store)
    session.accept_tokens(_tokens("old"))

    def failing_save(key, value):
        raise OSError("vault unreachable before mutation")

    with patch.object(store.secret_store, "save", side_effect=failing_save):
        with pytest.raises(OSError, match="vault unreachable before mutation"):
            session.accept_tokens(_tokens("new"))
    assert not store._credential_commit_path().exists()
    assert store.load() == _stored(_tokens("old"))
    assert session.state.refresh_token == "old-refresh"
    session.accept_tokens(_tokens("new"))
    assert store.load() == _stored(_tokens("new"))


@pytest.mark.skipif(os.name == "nt", reason="Synthetic secret-tool requires POSIX")
def test_provider_rejection_marked_uncertain_after_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv("R50_DB", str(tmp_path / "provider.json"))
    monkeypatch.setenv(
        "PATH", str(_install_tool(tmp_path)) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("R50_FAIL_AFTER_WRITE", "1")
    with linux_secret_store(tmp_path):
        store = PlatformSecretStore(kind="r50-probe")
        with pytest.raises(UncertainSecretStoreError) as failure:
            store.save("probe-key", b"probe-secret")
        assert "lost acknowledgement" in str(failure.value)
        assert is_infra_secure_store_error(failure.value)
        assert store.load("probe-key") == b"probe-secret"
