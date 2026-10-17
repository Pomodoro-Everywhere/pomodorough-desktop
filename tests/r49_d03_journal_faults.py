"""Inject failures only at real journal filesystem operations."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

from r49_d03_journal_support import JOURNAL, KEY, NEW, OLD, encode, memory, stored

from pomodorough import network
from pomodorough.secure_store import SecureStoreError


class JournalFault:
    def __init__(self, case, stage):
        self.case, self.stage = case, stage
        self.journal = case.store._credential_commit_path()
        self.files, self.events = {}, []
        self.attempt, self.phase = 0, "initial"
        self.real_mkstemp, self.real_replace = tempfile.mkstemp, os.replace
        self.real_unlink, self.real_fsync = Path.unlink, os.fsync
        self.provider_error = SecureStoreError("provider rejected save before mutation")
        self.abort_error = OSError(errno.EIO, "abort directory fsync failed")
        self.boundary_error = OSError(errno.EIO, f"{stage} failed")

    def install(self, patch):
        if self.stage.startswith(("abort-", "restore-", "provider-")):
            self.case.provider.failure = self.provider_error
        patch.setattr(network.tempfile, "mkstemp", self.mkstemp)
        patch.setattr(network.os, "replace", self.replace)
        patch.setattr(network.os, "fsync", self.fsync)
        patch.setattr(Path, "unlink", lambda path, *args, **kwargs:
                      self.unlink(path, *args, **kwargs))

    def event(self, name):
        # No session adoption is allowed before the filesystem boundary returns.
        assert memory(self.case.session) == self.case.before
        assert self.case.provider.load(KEY) == encode(stored(OLD))
        self.events.append(name)

    def mkstemp(self, *args, **kwargs):
        if kwargs.get("prefix") != f".{self.journal.name}.":
            return self.real_mkstemp(*args, **kwargs)
        self.attempt += 1
        label = "initial" if self.attempt == 1 else "restore"
        assert self.attempt <= 2
        if label == "restore":
            assert self.phase == "abort-failed"
            assert not self.journal.exists()
        if self.stage == f"{label}-tempfile":
            self.event(f"{label}-tempfile-failed")
            raise self.boundary_error
        descriptor, name = self.real_mkstemp(*args, **kwargs)
        metadata = os.fstat(descriptor)
        self.files[descriptor] = (metadata.st_dev, metadata.st_ino, Path(name), label)
        self.phase = f"{label}-tempfile"
        self.event(f"{label}-tempfile-created")
        return descriptor, name

    def fsync(self, descriptor):
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            return self.directory_fsync(descriptor)
        entry = self.files.get(descriptor)
        if entry is None or (metadata.st_dev, metadata.st_ino) != entry[:2]:
            return self.real_fsync(descriptor)
        _, _, path, label = entry
        assert path.read_bytes() == JOURNAL
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        if self.stage == f"{label}-file-fsync":
            self.event(f"{label}-file-fsync-failed")
            raise self.boundary_error
        self.real_fsync(descriptor)
        self.event(f"{label}-file-fsynced")

    def replace(self, source, destination, *args, **kwargs):
        if Path(destination) != self.journal:
            return self.real_replace(source, destination, *args, **kwargs)
        label = "initial" if self.attempt == 1 else "restore"
        assert Path(source).read_bytes() == JOURNAL
        assert self.events[-1] == f"{label}-file-fsynced"
        if self.stage == f"{label}-replace":
            self.event(f"{label}-replace-failed")
            raise self.boundary_error
        self.real_replace(source, destination, *args, **kwargs)
        assert self.journal.read_bytes() == JOURNAL
        assert stat.S_IMODE(self.journal.stat().st_mode) == 0o600
        assert not Path(source).exists()
        self.phase = f"{label}-installed"
        self.event(f"{label}-replaced")

    def directory_fsync(self, descriptor):
        if self.phase == "unlinked":
            assert not self.journal.exists()
            if self.stage != "provider-clean-abort":
                self.phase = "abort-failed"
                self.event("abort-directory-fsync-failed")
                raise self.abort_error
            self.real_fsync(descriptor)
            self.event("abort-directory-fsynced")
            return
        if self.phase.endswith("-installed"):
            label = self.phase.removesuffix("-installed")
            assert self.journal.read_bytes() == JOURNAL
            if self.stage == f"{label}-directory-fsync":
                self.event(f"{label}-directory-fsync-failed")
                raise self.boundary_error
            self.real_fsync(descriptor)
            self.phase = f"{label}-synced"
            self.event(f"{label}-directory-fsynced")
            return
        return self.real_fsync(descriptor)

    def unlink(self, path, *args, **kwargs):
        if path != self.journal:
            return self.real_unlink(path, *args, **kwargs)
        assert self.phase == "initial-synced"
        assert path.read_bytes() == JOURNAL
        assert self.case.provider.calls == [(KEY, encode(stored(NEW)))]
        if self.stage == "abort-unlink":
            self.event("abort-unlink-failed")
            raise self.boundary_error
        self.real_unlink(path, *args, **kwargs)
        assert not path.exists()
        self.phase = "unlinked"
        self.event("abort-unlinked")
