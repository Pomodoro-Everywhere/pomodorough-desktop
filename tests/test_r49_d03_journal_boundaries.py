"""Keep credential memory and replay consistent at actual journal boundaries."""

from __future__ import annotations

import os

import pytest
from r49_d03_journal_faults import JournalFault
from r49_d03_journal_support import (
    JOURNAL,
    KEY,
    NEW,
    OLD,
    CredentialCase,
    assert_recovery,
    disk,
    encode,
    exception_chain,
    expected_disk,
    expected_memory,
    memory,
    recover,
    stored,
)

from pomodorough.secure_store import SecureStoreError, TokenCommitPendingError

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_DIRECTORY"),
    reason="These regressions exercise POSIX replace, unlink, and directory fsync.",
)
INITIAL = ["initial-tempfile-created", "initial-file-fsynced", "initial-replaced"]
SYNCED = [*INITIAL, "initial-directory-fsynced"]
ABORTED = [*SYNCED, "abort-unlinked", "abort-directory-fsync-failed"]
RESTORED = [*ABORTED, "restore-tempfile-created", "restore-file-fsynced", "restore-replaced"]


@pytest.fixture(params=[False, True], ids=["no-fallback", "old-fallback"])
def credentials(request, tmp_path):
    return CredentialCase(tmp_path, request.param), request.param


@pytest.fixture(params=[False, True], ids=["same-process-first", "fresh-process-first"])
def process_first(request):
    return request.param


def exercise(case, stage, monkeypatch):
    fault = JournalFault(case, stage)
    try:
        with monkeypatch.context() as patch:
            fault.install(patch)
            with pytest.raises(OSError) as caught:
                case.session.accept_tokens(NEW)
            observed = (disk(case.root), memory(case.session), list(case.provider.calls))
    finally:
        case.provider.failure = None
    assert not list(case.root.glob(f".{fault.journal.name}.*"))
    return fault, caught.value, observed


def assert_consistent(case, observed, tokens, fallback, process_first):
    at_error, session_at_error, _calls = observed
    installed = tokens == NEW
    # Read every recovery path before checking outcomes, including on historical sources.
    observations = recover(case, process_first=process_first)
    assert_recovery(case, observations, tokens, fallback and not installed)
    case.assert_memory(tokens)
    assert at_error == expected_disk(OLD, fallback, journal=JOURNAL if installed else None)
    assert session_at_error == expected_memory(case.before, tokens)


def assert_pending(error, cause):
    assert type(error) is TokenCommitPendingError
    assert error.during_recovery is False
    assert str(error) == "Secure credential commit is pending; retry."
    assert error.__cause__ is cause


@pytest.mark.parametrize("stage,events", [
    ("initial-file-fsync", ["initial-tempfile-created", "initial-file-fsync-failed"]),
    ("initial-replace", ["initial-tempfile-created", "initial-file-fsynced", "initial-replace-failed"]),
])
def test_preinstallation_failure_keeps_complete_old_credentials(
    stage, events, credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, stage, monkeypatch)
    assert_consistent(case, observed, OLD, fallback, process_first)
    assert observed[2] == []
    assert error is fault.boundary_error
    assert fault.events == events


def test_installed_journal_directory_fsync_failure_replays_complete_new_credentials(
    credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, "initial-directory-fsync", monkeypatch)
    assert_consistent(case, observed, NEW, fallback, process_first)
    assert observed[2] == []
    assert_pending(error, fault.boundary_error)
    assert fault.events == [*INITIAL, "initial-directory-fsync-failed"]


def test_provider_failure_with_durable_abort_keeps_complete_old_credentials(
    credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, "provider-clean-abort", monkeypatch)
    assert_consistent(case, observed, OLD, fallback, process_first)
    assert observed[2] == [(KEY, encode(stored(NEW)))]
    assert error is fault.provider_error
    assert fault.events == [*SYNCED, "abort-unlinked", "abort-directory-fsynced"]


def test_abort_unlink_failure_retains_complete_replay_and_new_session(
    credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, "abort-unlink", monkeypatch)
    assert_consistent(case, observed, NEW, fallback, process_first)
    assert observed[2] == [(KEY, encode(stored(NEW)))]
    assert_pending(error, fault.boundary_error)
    assert any(item is fault.provider_error for item in exception_chain(error))
    assert fault.events == [*SYNCED, "abort-unlink-failed"]


def test_abort_unlink_then_directory_fsync_failure_restores_before_session_adoption(
    credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, "abort-directory-fsync", monkeypatch)
    assert_consistent(case, observed, NEW, fallback, process_first)
    assert observed[2] == [(KEY, encode(stored(NEW)))]
    assert_pending(error, fault.abort_error)
    assert any(item is fault.provider_error for item in exception_chain(error))
    assert fault.events == [*RESTORED, "restore-directory-fsynced"]


@pytest.mark.parametrize("stage,events", [
    ("restore-tempfile", [*ABORTED, "restore-tempfile-failed"]),
    ("restore-file-fsync", [*ABORTED, "restore-tempfile-created", "restore-file-fsync-failed"]),
    ("restore-replace", [*ABORTED, "restore-tempfile-created", "restore-file-fsynced", "restore-replace-failed"]),
])
def test_restoration_failure_before_replace_preserves_old_restart_and_session(
    stage, events, credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, stage, monkeypatch)
    assert_consistent(case, observed, OLD, fallback, process_first)
    assert observed[2] == [(KEY, encode(stored(NEW)))]
    assert type(error) is SecureStoreError
    assert str(error) == (
        "Credential commit abort durability is uncertain; journal restoration failed; retry."
    )
    assert error.__cause__ is fault.boundary_error
    chain = exception_chain(error)
    assert any(item is fault.abort_error for item in chain)
    assert any(item is fault.provider_error for item in chain)
    assert fault.events == events


def test_restoration_directory_fsync_failure_keeps_installed_journal_replayable(
    credentials, process_first, monkeypatch,
):
    case, fallback = credentials
    fault, error, observed = exercise(case, "restore-directory-fsync", monkeypatch)
    assert_consistent(case, observed, NEW, fallback, process_first)
    assert observed[2] == [(KEY, encode(stored(NEW)))]
    assert_pending(error, fault.boundary_error)
    chain = exception_chain(error)
    assert any(item is fault.abort_error for item in chain)
    assert any(item is fault.provider_error for item in chain)
    assert fault.events == [*RESTORED, "restore-directory-fsync-failed"]
