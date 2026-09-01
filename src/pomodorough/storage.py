from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

from .core import (
    ACTIVE_STATUSES,
    PHASES,
    elapsed_ms,
    parse_timestamp_ms,
    project_auto_start_breaks,
    task_from_title,
)
from .secure_store import PlatformSecretStore, SecretMutationJournal
from .shared_core import (
    ProjectionApplyV2,
    SharedCore as SharedCore,
    SharedCoreDispatcher,
    SharedCoreError,
    apply_projection_v2,
)
from .storage_canonical import (
    CanonicalResponseStorage,
    CanonicalStorageDependencies,
    valid_canonical_timer,
    valid_history_item,
)
from .storage_iroh_records import IrohRecordPersistence
from .storage_generation import GenerationReservation
from .storage_completion import TimerCompletionPolicy

from .storage_model import (
    ACKNOWLEDGEMENT_OUTCOMES as ACKNOWLEDGEMENT_OUTCOMES,
    CANONICAL_DURATION_MAX_MS as CANONICAL_DURATION_MAX_MS,
    COMMAND_TYPES as COMMAND_TYPES,
    DURATION_MIN_MS as DURATION_MIN_MS,
    MAX_CLOCK_CONTINUITY_DRIFT_MS as MAX_CLOCK_CONTINUITY_DRIFT_MS,
    MAX_CLOCK_SKEW_MS as MAX_CLOCK_SKEW_MS,
    MAX_SAFE_INTEGER as MAX_SAFE_INTEGER,
    MAX_SERVER_TIME_UNCERTAINTY_MS as MAX_SERVER_TIME_UNCERTAINTY_MS,
    PREFERENCE_DURATION_MAX_MS as PREFERENCE_DURATION_MAX_MS,
    RESOLUTION_OPERATION_MAX as RESOLUTION_OPERATION_MAX,
    _default_shared_core as _default_shared_core,
    utc_timestamp as utc_timestamp,
)
from .storage_replication import ReplicationStorage, _iroh_conflict_time_ms
from .storage_sync import SyncStorage, SyncStorageDependencies
from .storage_workspace import WorkspacePersistence
from .uuid7 import reserve_uuid7, uuid7_parts

_LOCAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_commands (
    id TEXT PRIMARY KEY,
    device_sequence INTEGER NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    depends_on_command_id TEXT
);
CREATE TABLE IF NOT EXISTS pending_task_operations (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_duration_operations (
    id TEXT PRIMARY KEY,
    phase TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auto_start_operations (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_selected_task_operations (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auto_breaks (
    finish_command_id TEXT PRIMARY KEY,
    timer_id TEXT NOT NULL,
    finish_device_sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_auto_break_starts (
    source_finish_command_id TEXT PRIMARY KEY,
    source_timer_id TEXT NOT NULL,
    start_command_id TEXT NOT NULL UNIQUE,
    selected_phase_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_phase_advances (
    finish_command_id TEXT PRIMARY KEY,
    timer_id TEXT NOT NULL,
    source_phase TEXT NOT NULL,
    advanced_phase TEXT NOT NULL,
    selected_phase_version INTEGER NOT NULL
);
"""

_IROH_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS iroh_rooms (
        room_id TEXT PRIMARY KEY,
        room_secret BLOB NOT NULL,
        room_name TEXT,
        return_workspace TEXT NOT NULL,
        workspace TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL,
        conflict TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS iroh_records (
        room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
        domain TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        device_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        record TEXT NOT NULL,
        PRIMARY KEY(room_id, domain, operation_id)
    )""",
    """CREATE TABLE IF NOT EXISTS iroh_peers (
        room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
        endpoint_id TEXT NOT NULL,
        endpoint_ticket TEXT NOT NULL,
        device_id TEXT,
        display_name TEXT,
        last_seen_at_ms INTEGER,
        PRIMARY KEY(room_id, endpoint_id)
    )""",
    """CREATE TABLE IF NOT EXISTS iroh_conflicts (
        room_id TEXT NOT NULL REFERENCES iroh_rooms(room_id) ON DELETE CASCADE,
        domain TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        local_digest TEXT NOT NULL,
        received_digest TEXT NOT NULL,
        received_record TEXT,
        detected_at_ms INTEGER NOT NULL,
        PRIMARY KEY(room_id, domain, operation_id, received_digest)
    )""",
    """CREATE INDEX IF NOT EXISTS iroh_records_inventory
        ON iroh_records(room_id, domain, operation_id)""",
    """CREATE INDEX IF NOT EXISTS iroh_peers_recent
        ON iroh_peers(room_id, last_seen_at_ms DESC)""",
)


@dataclass(frozen=True)
class _ResponseTiming:
    server_ms: int
    request_physical_ms: int
    received_physical_ms: int
    request_monotonic_ms: int
    received_monotonic_ms: int
    round_trip_ms: int
    clock_disagreement_ms: int


@dataclass(frozen=True)
class _CommandQueueContext:
    effective_now_ms: int
    timer: dict[str, Any] | None
    selected_task_id: str | None
    generates_break: bool


@dataclass(frozen=True)
class _AutoBreakTrigger:
    finish_command_id: str
    timer_id: str
    finish_sequence: int


@dataclass(frozen=True)
class _AutoBreakContext:
    settings: dict[str, Any]
    generated_phase: str | None
    source_already_accepted: bool


def default_data_path() -> Path:
    return user_data_path("pomodorough", appauthor=False) / "pomodorough.sqlite3"


class Store:
    def __init__(
        self,
        path: Path | None = None,
        *,
        iroh_secret_store: PlatformSecretStore | None = None,
        shared_core: SharedCoreDispatcher | None = None,
    ) -> None:
        self._trusted_time_anchor: dict[str, int] | None = None
        self._timer_time_anchor: dict[str, Any] | None = None
        uses_default_path = path is None
        self.path = default_data_path() if uses_default_path else path
        self._iroh_secret_store = iroh_secret_store or PlatformSecretStore()
        self._shared_core = shared_core
        self._open_database(restrict_existing_parent=uses_default_path)
        self._create_local_schema()
        self._migrate_local_schema()
        if self._migrated_iroh_capabilities:
            self.connection.execute("VACUUM")
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._initialize()
        self._configure_storage_responsibilities()
        self._restore_trusted_time_anchor()

    def _configure_storage_responsibilities(self) -> None:
        self._generation_storage = GenerationReservation(
            self.get_meta,
            self._trusted_now_ms,
            lambda: self._shared_core,
        )
        self._completion_policy = TimerCompletionPolicy(
            lambda: self._shared_core,
            lambda: self.device_id,
            lambda: self.replication_mode,
            self.get_meta,
        )
        self._workspace_storage = WorkspacePersistence(
            self.connection,
            self.get_meta,
            self._set_meta,
            self._normalize_settings,
        )
        self._iroh_record_storage = IrohRecordPersistence(
            self.connection,
            _iroh_conflict_time_ms,
        )
        self._sync_storage = SyncStorage(self._sync_dependencies())
        self._canonical_storage = CanonicalResponseStorage(
            self._canonical_dependencies()
        )
        self._replication_storage = ReplicationStorage(
            self,
            self._workspace_storage,
            self._iroh_record_storage,
        )

    def _sync_dependencies(self) -> SyncStorageDependencies:
        return SyncStorageDependencies(
            connection=self.connection,
            device_id=lambda: self.device_id,
            shared_core=lambda: self._shared_core,
            validate_integer=self._bounded_integer,
            response_clock_sample=self._clock_sample_for_response,
            transaction=self._immediate_transaction,
            preflight_pending_queues=self._preflight_pending_queues,
            project_operation=self._project_operation,
            write_meta=self._set_meta,
            set_trusted_time_anchor=self._set_trusted_time_anchor,
            validate_sync_response=lambda response, request: (
                self._canonical_storage._validated_sync_response(response, request)
            ),
            read_meta=self.get_meta,
            load_state=self.load,
            replace_meta=self.set_meta,
        )

    def _canonical_dependencies(self) -> CanonicalStorageDependencies:
        return CanonicalStorageDependencies(
            connection=self.connection,
            device_id=self.device_id,
            shared_core=lambda: self._shared_core,
            _canonical_durations=self._canonical_durations,
            _duration_ms=self._duration_ms,
            _logical_clock=self._logical_clock,
            _physical_time_ms=self._physical_time_ms,
            _normalize_settings=self._normalize_settings,
            _set_meta=self._set_meta,
            get_meta=self.get_meta,
            _clock_sample_for_response=lambda *args: (
                self._clock_sample_for_response(*args)
            ),
            _display_minutes=self._display_minutes,
            _ensure_no_pending_resolution=(
                self._sync_storage._ensure_no_pending_resolution
            ),
            _immediate_transaction=self._immediate_transaction,
            _preflight_pending_queues=self._preflight_pending_queues,
            _project_operation=self._project_operation,
            _prune_command_physical_times=self._prune_command_physical_times,
            _set_trusted_time_anchor=lambda anchor: self._set_trusted_time_anchor(anchor),
            pending_resolution=self._sync_storage.pending_resolution,
            pending_sync=self._sync_storage.pending_sync,
            _command_physical_times=self._command_physical_times,
            _validated_projection_state=self._validated_projection_state,
        )

    def _open_database(self, *, restrict_existing_parent: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if restrict_existing_parent:
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")

    def _create_local_schema(self) -> None:
        self.connection.executescript(_LOCAL_SCHEMA_SQL)
        self.connection.commit()

    def _migrate_local_schema(self) -> None:
        with (
            SecretMutationJournal(self._iroh_secret_store) as secrets,
            self._immediate_transaction(),
        ):
            self._migrate_pending_command_dependency()
            self._migrate_auto_break_phase_version()
            for statement in _IROH_SCHEMA_STATEMENTS:
                self.connection.execute(statement)
            self._migrated_iroh_capabilities = (
                self._migrate_plaintext_iroh_capabilities(secrets)
            )
            self._set_meta("irohSchemaVersion", 1)

    def _migrate_pending_command_dependency(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(pending_commands)")
        }
        if "depends_on_command_id" not in columns:
            self.connection.execute(
                "ALTER TABLE pending_commands ADD COLUMN depends_on_command_id TEXT"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS pending_commands_dependency "
            "ON pending_commands(depends_on_command_id)"
        )

    def _migrate_auto_break_phase_version(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(pending_auto_break_starts)"
            )
        }
        if "selected_phase_version" not in columns:
            self.connection.execute(
                "ALTER TABLE pending_auto_break_starts ADD COLUMN "
                "selected_phase_version INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.connection.close()

    def sync_payload(self) -> dict[str, Any]:
        return self._sync_storage.sync_payload()

    def pending_sync(self) -> dict[str, Any] | None:
        return self._sync_storage.pending_sync()

    def has_sendable_sync_operations(self) -> bool:
        return self._sync_storage.has_sendable_sync_operations()

    def pending_resolution(
        self, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return self._sync_storage.pending_resolution(user_id)

    def clear_pending_resolution(self) -> None:
        self._sync_storage.clear_pending_resolution()

    def _ensure_no_pending_resolution(self) -> None:
        self._sync_storage._ensure_no_pending_resolution()

    def discard_pending_resolution(self, user_id: str, request_id: str) -> bool:
        return self._sync_storage.discard_pending_resolution(user_id, request_id)

    def bootstrap_resolution_plan(
        self,
        response: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> dict[str, Any]:
        return self._sync_storage.bootstrap_resolution_plan(
            response,
            request_physical_ms=request_physical_ms,
            received_physical_ms=received_physical_ms,
            request_monotonic_ms=request_monotonic_ms,
            received_monotonic_ms=received_monotonic_ms,
        )

    def prepare_resolution(
        self,
        user: dict[str, Any],
        expected_revision: int,
        strategy: str,
    ) -> dict[str, Any]:
        return self._sync_storage.prepare_resolution(
            user, expected_revision, strategy
        )

    def _validated_sync_response(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        return self._canonical_storage._validated_sync_response(response, request)

    @classmethod
    def _valid_canonical_timer(cls, timer: Any) -> bool:
        return valid_canonical_timer(timer, cls._duration_ms)

    @classmethod
    def _valid_history_item(cls, item: Any) -> bool:
        return valid_history_item(item, cls._duration_ms)

    def apply_sync(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        return self._canonical_storage.apply_sync(
            response,
            request,
            request_physical_ms=request_physical_ms,
            received_physical_ms=received_physical_ms,
            request_monotonic_ms=request_monotonic_ms,
            received_monotonic_ms=received_monotonic_ms,
        )

    def apply_resolution(
        self,
        response: dict[str, Any],
        user: dict[str, Any],
        request_id: str | None = None,
        *,
        request_physical_ms: int | None = None,
        received_physical_ms: int | None = None,
        request_monotonic_ms: int | None = None,
        received_monotonic_ms: int | None = None,
    ) -> list[str]:
        return self._canonical_storage.apply_resolution(
            response,
            user,
            request_id,
            request_physical_ms=request_physical_ms,
            received_physical_ms=received_physical_ms,
            request_monotonic_ms=request_monotonic_ms,
            received_monotonic_ms=received_monotonic_ms,
        )

    @property
    def replication_mode(self) -> str:
        return self._replication_storage.replication_mode

    @property
    def active_iroh_room_id(self) -> str | None:
        return self._replication_storage.active_iroh_room_id

    def create_iroh_room(
        self,
        room_secret: bytes,
        room_name: str | None = None,
        *,
        now_ms: int | None = None,
    ) -> str:
        return self._replication_storage.create_iroh_room(
            room_secret, room_name, now_ms=now_ms
        )

    def prepare_iroh_join(
        self,
        room_id: str,
        room_secret: bytes,
        room_name: str | None,
        endpoint_id: str,
        endpoint_ticket: str,
        *,
        now_ms: int | None = None,
    ) -> None:
        self._replication_storage.prepare_iroh_join(
            room_id,
            room_secret,
            room_name,
            endpoint_id,
            endpoint_ticket,
            now_ms=now_ms,
        )

    def discard_inactive_iroh_room(self, room_id: str) -> None:
        self._replication_storage.discard_inactive_iroh_room(room_id)

    def activate_joined_iroh_room(self, room_id: str) -> None:
        self._replication_storage.activate_joined_iroh_room(room_id)

    def set_replication_mode(self, mode: str) -> None:
        self._replication_storage.set_replication_mode(mode)

    def leave_iroh_room(self) -> None:
        self._replication_storage.leave_iroh_room()

    def iroh_room(self, room_id: str | None = None) -> dict[str, Any] | None:
        return self._replication_storage.iroh_room(room_id)

    def iroh_room_secret(self, room_id: str) -> bytes:
        return self._replication_storage.iroh_room_secret(room_id)

    def capture_local_iroh_records(self) -> bool:
        return self._replication_storage.capture_local_iroh_records()

    def _capture_iroh_after_mutation(self) -> None:
        self._replication_storage._capture_iroh_after_mutation()

    def _capture_iroh_after_mutation_locked(self) -> None:
        self._replication_storage._capture_iroh_after_mutation_locked()

    def _capture_local_iroh_records_locked(self, room_id: str) -> bool:
        return self._replication_storage._capture_local_iroh_records_locked(room_id)

    def project_iroh_expiry(self, now_ms: int | None = None) -> bool:
        return self._replication_storage.project_iroh_expiry(now_ms)

    def insert_remote_iroh_records(
        self,
        room_id: str,
        records: list[dict[str, Any]],
        advertised_digests: dict[tuple[str, str], str] | None = None,
    ) -> bool:
        return self._replication_storage.insert_remote_iroh_records(
            room_id, records, advertised_digests
        )

    def iroh_inventory(
        self, room_id: str, after: str | None, limit: int
    ) -> tuple[list[dict[str, str]], str | None]:
        return self._iroh_record_storage.inventory(room_id, after, limit)

    def iroh_operations(
        self, room_id: str, references: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        return self._iroh_record_storage.operations(room_id, references)

    def missing_iroh_references(
        self, room_id: str, remote_entries: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        return self._replication_storage.missing_iroh_references(
            room_id, remote_entries
        )

    def upsert_iroh_peer(
        self,
        room_id: str,
        endpoint_id: str,
        endpoint_ticket: str,
        device_id: str | None,
        display_name: str | None,
        last_seen_at_ms: int | None = None,
    ) -> None:
        self._replication_storage.upsert_iroh_peer(
            room_id,
            endpoint_id,
            endpoint_ticket,
            device_id,
            display_name,
            last_seen_at_ms,
        )

    def iroh_peers(self, room_id: str) -> list[dict[str, Any]]:
        return self._replication_storage.iroh_peers(room_id)

    def has_pending_auto_break(self) -> bool:
        return self._replication_storage.has_pending_auto_break()

    def _projected_local_genesis(self) -> dict[str, Any]:
        return self._replication_storage._projected_local_genesis()

    @staticmethod
    def _workspace_table_columns() -> dict[str, tuple[str, ...]]:
        return WorkspacePersistence.table_columns()

    def _capture_workspace(self) -> dict[str, Any]:
        return self._workspace_storage.capture()

    def _restore_workspace(self, workspace: dict[str, Any]) -> None:
        self._workspace_storage.restore(workspace)

    def _restore_workspace_metadata(self, metadata: dict[str, Any]) -> None:
        self._workspace_storage.restore_metadata(metadata)

    def _workspace_without_account(
        self, workspace: dict[str, Any], *, preserve_domain: bool
    ) -> dict[str, Any]:
        return self._workspace_storage.without_account(
            workspace, preserve_domain=preserve_domain
        )

    def _save_iroh_workspace(
        self, room_id: str, workspace: dict[str, Any]
    ) -> None:
        self._workspace_storage.save_room(room_id, workspace)

    @staticmethod
    def _room_secret_key(room_id: str) -> str:
        return f"room-secret:{room_id}"

    @staticmethod
    def _peer_ticket_key(room_id: str, endpoint_id: str) -> str:
        return f"peer-ticket:{room_id}:{endpoint_id}"

    @staticmethod
    def _secure_reference(key: str) -> bytes:
        return f"secure:{key}".encode("utf-8")

    def _migrate_plaintext_iroh_capabilities(
        self, secrets: SecretMutationJournal
    ) -> bool:
        migrated = False
        for row in self.connection.execute(
            "SELECT room_id, room_secret FROM iroh_rooms"
        ).fetchall():
            secret = row["room_secret"]
            if not isinstance(secret, bytes) or len(secret) != 32:
                continue
            room_id = str(row["room_id"])
            key = self._room_secret_key(room_id)
            secrets.save(key, bytes(secret))
            self.connection.execute(
                "UPDATE iroh_rooms SET room_secret = ? WHERE room_id = ?",
                (self._secure_reference(key), room_id),
            )
            migrated = True
        for row in self.connection.execute(
            "SELECT room_id, endpoint_id, endpoint_ticket FROM iroh_peers"
        ).fetchall():
            ticket = row["endpoint_ticket"]
            if not isinstance(ticket, str) or ticket.startswith("secure:"):
                continue
            room_id = str(row["room_id"])
            endpoint_id = str(row["endpoint_id"])
            key = self._peer_ticket_key(room_id, endpoint_id)
            secrets.save(key, ticket.encode("utf-8"))
            self.connection.execute(
                "UPDATE iroh_peers SET endpoint_ticket = ? "
                "WHERE room_id = ? AND endpoint_id = ?",
                (f"secure:{key}", room_id, endpoint_id),
            )
            migrated = True
        if migrated:
            self.connection.execute("PRAGMA secure_delete=ON")
        return migrated

    def _initialize(self) -> None:
        with self._immediate_transaction():
            for key, value in self._initial_meta_defaults().items():
                self.connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                    (key, json.dumps(value, separators=(",", ":"))),
                )
            settings = self._normalize_settings(self.get_meta("settings", {}))
            self._set_meta("settings", settings)
            snapshot, snapshot_had_auto_start = self._initialize_snapshot()
            self._migrate_legacy_preferences(settings, snapshot, snapshot_had_auto_start)
            self._reconcile_auto_start_setting(settings, snapshot)
            self._repair_command_physical_times()

    @staticmethod
    def _initial_meta_defaults() -> dict[str, Any]:
        return {
            "deviceId": f"desktop-{uuid.uuid4()}",
            "deviceSequence": 0,
            "hlc": {"wallMs": 0, "counter": 0},
            "lastUuidV7": None,
            "serverClockSample": None,
            "commandPhysicalTimes": {},
            "selectedPhaseVersion": 0,
            "settings": {
                "selectedPhase": "focus",
                "durations": {
                    phase: definition["default_minutes"]
                    for phase, definition in PHASES.items()
                },
                "durationsMs": {
                    phase: definition["default_minutes"] * 60_000
                    for phase, definition in PHASES.items()
                },
                "autoStartBreaks": False,
                "selectedTaskId": None,
            },
            "snapshot": {
                "revision": 0,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "knownTasks": [],
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "user": None,
            },
            "pendingSync": None,
            "pendingResolution": None,
            "replicationMode": "centralized",
            "activeIrohRoomId": None,
        }

    def _initialize_snapshot(self) -> tuple[dict[str, Any], bool]:
        snapshot = self.get_meta("snapshot", {})
        had_auto_start = "autoStartBreaks" in snapshot
        changed = False
        for key, value in (("tasks", []), ("knownTasks", []),
                           ("autoStartBreaks", False), ("selectedTaskId", None)):
            if key not in snapshot:
                snapshot[key] = value
                changed = True
        if changed:
            self._set_meta("snapshot", snapshot)
        return snapshot, had_auto_start

    def _migrate_legacy_preferences(
        self, settings: dict[str, Any], snapshot: dict[str, Any],
        snapshot_had_auto_start: bool,
    ) -> None:
            if not self.get_meta("durationMigrationComplete", False):
                for phase, definition in PHASES.items():
                    duration_ms = int(settings["durationsMs"][phase])
                    if duration_ms != definition["default_minutes"] * 60_000:
                        self._queue_duration_operation(
                            phase, duration_ms, settings, 0, bootstrap=True
                        )
                self._set_meta("durationMigrationComplete", True)
            if not self.get_meta("autoStartMigrationComplete", False):
                if settings["autoStartBreaks"]:
                    self._queue_auto_start_operation(True, settings, 0, bootstrap=True)
                self._set_meta(
                    "autoStartLegacyDefaultUnknown",
                    not settings["autoStartBreaks"]
                    and not snapshot_had_auto_start
                    and not self._pending_auto_start_operations(),
                )
                self._set_meta("autoStartMigrationComplete", True)
            if not self.get_meta("selectedTaskMigrationComplete", False):
                selected_task_id = settings.get("selectedTaskId")
                if selected_task_id is None:
                    self._set_meta("selectedTaskMigrationComplete", True)
                elif (
                    self.get_meta("pendingResolution") is None
                    and self.get_meta("replicationMode", "centralized") != "iroh"
                ):
                    self._queue_selected_task_operation(
                        selected_task_id, settings, 0, bootstrap=True
                    )
                    self._set_meta("selectedTaskMigrationComplete", True)

    def _reconcile_auto_start_setting(
        self, settings: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
            pending_auto_starts = self._pending_auto_start_operations()
            projected = project_auto_start_breaks(
                bool(snapshot["autoStartBreaks"]), pending_auto_starts
            )
            if settings["autoStartBreaks"] != projected:
                settings["autoStartBreaks"] = projected
                self._set_meta("settings", settings)

    def _repair_command_physical_times(self) -> None:
            physical_times = self.get_meta("commandPhysicalTimes", {})
            if not isinstance(physical_times, dict):
                physical_times = {}
            pending_ids = set()
            for row in self.connection.execute(
                "SELECT id, payload FROM pending_commands"
            ):
                command_id = str(row["id"])
                pending_ids.add(command_id)
                if command_id in physical_times:
                    continue
                try:
                    command = json.loads(row["payload"])
                    occurred_ms = parse_timestamp_ms(command.get("occurredAt"))
                except (AttributeError, TypeError, json.JSONDecodeError):
                    occurred_ms = None
                if occurred_ms is not None:
                    physical_times[command_id] = occurred_ms
            physical_times = {
                command_id: value
                for command_id, value in physical_times.items()
                if command_id in pending_ids
            }
            self._set_meta("commandPhysicalTimes", physical_times)

    @staticmethod
    def _normalize_settings(settings: Any) -> dict[str, Any]:
        normalized = dict(settings) if isinstance(settings, dict) else {}
        durations = normalized.get("durations")
        durations = durations if isinstance(durations, dict) else {}
        durations_ms = normalized.get("durationsMs")
        durations_ms = durations_ms if isinstance(durations_ms, dict) else {}
        normalized_durations_ms: dict[str, int] = {}
        for phase, definition in PHASES.items():
            default_minutes = int(definition["default_minutes"])
            value = durations_ms.get(phase)
            try:
                duration_ms = int(value) if not isinstance(value, bool) else 0
            except (TypeError, ValueError):
                duration_ms = 0
            if (
                not DURATION_MIN_MS <= duration_ms <= PREFERENCE_DURATION_MAX_MS
                or duration_ms % 60_000
            ):
                legacy_value = durations.get(phase, default_minutes)
                try:
                    minutes = (
                        int(legacy_value) if not isinstance(legacy_value, bool) else 0
                    )
                except (TypeError, ValueError):
                    minutes = 0
                duration_ms = (
                    minutes * 60_000
                    if 1 <= minutes <= 180
                    else default_minutes * 60_000
                )
            normalized_durations_ms[phase] = duration_ms
        normalized["durationsMs"] = normalized_durations_ms
        normalized["durations"] = {
            phase: Store._display_minutes(duration_ms)
            for phase, duration_ms in normalized_durations_ms.items()
        }
        if normalized.get("selectedPhase") not in PHASES:
            normalized["selectedPhase"] = "focus"
        normalized["autoStartBreaks"] = bool(normalized.get("autoStartBreaks", False))
        normalized.setdefault("selectedTaskId", None)
        return normalized

    @staticmethod
    def _display_minutes(duration_ms: int) -> int:
        return duration_ms // 60_000

    @staticmethod
    def _duration_ms(value: Any, *, maximum: int = PREFERENCE_DURATION_MAX_MS) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Duration must be an integer number of milliseconds.")
        if not DURATION_MIN_MS <= value <= maximum:
            raise ValueError(
                f"Duration must be between {DURATION_MIN_MS} and {maximum} milliseconds."
            )
        if value % 60_000:
            raise ValueError("Duration must be measured in whole minutes.")
        return value

    @staticmethod
    def _bounded_integer(value: Any, label: str, *, minimum: int = 0) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= MAX_SAFE_INTEGER
        ):
            raise ValueError(f"{label} is outside the safe integer range.")
        return value

    @classmethod
    def _physical_time_ms(cls, value: Any) -> int:
        return cls._bounded_integer(value, "Physical occurrence time", minimum=1)

    @staticmethod
    def _signed_safe_integer(value: Any, label: str) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
        ):
            raise ValueError(f"{label} is outside the safe integer range.")
        return value

    @classmethod
    def _server_clock_sample(cls, value: Any) -> dict[str, int] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Persisted server clock sample is invalid.")
        uncertainty_ms = cls._bounded_integer(
            value.get("uncertaintyMs"), "Persisted server clock uncertainty"
        )
        if uncertainty_ms > MAX_SERVER_TIME_UNCERTAINTY_MS:
            raise ValueError("Persisted server clock uncertainty is too large.")
        if set(value) != {
            "offsetMs",
            "uncertaintyMs",
            "acquiredPhysicalMs",
            "acquiredMonotonicMs",
            "acquiredTrustedMs",
        }:
            raise ValueError("Persisted server clock sample is invalid.")
        offset_ms = cls._signed_safe_integer(
            value.get("offsetMs"), "Persisted server clock offset"
        )
        acquired_physical_ms = cls._physical_time_ms(value.get("acquiredPhysicalMs"))
        acquired_monotonic_ms = cls._bounded_integer(
            value.get("acquiredMonotonicMs"),
            "Persisted server clock monotonic acquisition",
        )
        acquired_trusted_ms = cls._physical_time_ms(value.get("acquiredTrustedMs"))
        return {
            "offsetMs": offset_ms,
            "uncertaintyMs": uncertainty_ms,
            "acquiredPhysicalMs": acquired_physical_ms,
            "acquiredMonotonicMs": acquired_monotonic_ms,
            "acquiredTrustedMs": acquired_trusted_ms,
        }

    @classmethod
    def _projected_trusted_time(
        cls,
        sample: dict[str, int],
        physical_ms: int,
        monotonic_ms: int,
    ) -> int | None:
        physical_ms = cls._physical_time_ms(physical_ms)
        monotonic_ms = cls._bounded_integer(monotonic_ms, "Monotonic clock")
        if monotonic_ms < sample["acquiredMonotonicMs"]:
            return None
        elapsed_ms = monotonic_ms - sample["acquiredMonotonicMs"]
        expected_physical_ms = sample["acquiredPhysicalMs"] + elapsed_ms
        if abs(physical_ms - expected_physical_ms) > MAX_CLOCK_CONTINUITY_DRIFT_MS:
            return None
        return cls._physical_time_ms(sample["acquiredTrustedMs"] + elapsed_ms)

    def _restore_trusted_time_anchor(self) -> None:
        with self._immediate_transaction():
            try:
                sample = self._server_clock_sample(self.get_meta("serverClockSample"))
            except ValueError:
                self._set_meta("serverClockSample", None)
                return
            if sample is None:
                return
            physical_ms = self._physical_time_ms(int(time.time() * 1000))
            monotonic_ms = self._bounded_integer(
                time.monotonic_ns() // 1_000_000, "Monotonic clock"
            )
            if self._projected_trusted_time(sample, physical_ms, monotonic_ms) is None:
                self._set_meta("serverClockSample", None)
                return
            self._trusted_time_anchor = sample

    def _clock_sample_for_response(
        self,
        server_time_ms: int,
        request_physical_ms: int | None,
        received_physical_ms: int | None,
        request_monotonic_ms: int | None,
        received_monotonic_ms: int | None,
    ) -> tuple[dict[str, int] | None, dict[str, int] | None]:
        timings = (
            request_physical_ms,
            received_physical_ms,
            request_monotonic_ms,
            received_monotonic_ms,
        )
        if all(value is None for value in timings):
            return None, None
        if any(value is None for value in timings):
            raise ValueError("Local response timing is incomplete.")
        timing = self._validated_response_timing(server_time_ms, timings)
        midpoint_elapsed_ms = timing.round_trip_ms // 2
        uncertainty_ms = (
            timing.round_trip_ms + 1
        ) // 2 + timing.clock_disagreement_ms
        if uncertainty_ms > MAX_SERVER_TIME_UNCERTAINTY_MS:
            raise ValueError("Server time sample uncertainty is too large.")
        midpoint_ms = self._physical_time_ms(
            timing.request_physical_ms + midpoint_elapsed_ms
        )
        offset_ms = self._signed_safe_integer(
            timing.server_ms - midpoint_ms, "Server clock offset"
        )
        acquired_trusted_ms = self._physical_time_ms(
            timing.server_ms + timing.round_trip_ms - midpoint_elapsed_ms
        )
        sample = {
            "offsetMs": offset_ms,
            "uncertaintyMs": uncertainty_ms,
            "acquiredPhysicalMs": timing.received_physical_ms,
            "acquiredMonotonicMs": timing.received_monotonic_ms,
            "acquiredTrustedMs": acquired_trusted_ms,
        }
        return sample, sample

    def _validated_response_timing(
        self,
        server_time_ms: int,
        timings: tuple[int | None, int | None, int | None, int | None],
    ) -> _ResponseTiming:
        server_ms = self._physical_time_ms(server_time_ms)
        request_physical_ms = self._physical_time_ms(timings[0])
        received_physical_ms = self._physical_time_ms(timings[1])
        request_monotonic_ms = self._bounded_integer(
            timings[2], "Request monotonic clock"
        )
        received_monotonic_ms = self._bounded_integer(
            timings[3], "Response monotonic clock"
        )
        if received_monotonic_ms < request_monotonic_ms:
            raise ValueError("Local response timing is invalid.")
        round_trip_ms = received_monotonic_ms - request_monotonic_ms
        physical_round_trip_ms = received_physical_ms - request_physical_ms
        clock_disagreement_ms = abs(physical_round_trip_ms - round_trip_ms)
        if (
            physical_round_trip_ms < 0
            or clock_disagreement_ms > MAX_CLOCK_CONTINUITY_DRIFT_MS
        ):
            raise ValueError("Local response clocks disagree.")
        return _ResponseTiming(
            server_ms,
            request_physical_ms,
            received_physical_ms,
            request_monotonic_ms,
            received_monotonic_ms,
            round_trip_ms,
            clock_disagreement_ms,
        )

    def _set_trusted_time_anchor(self, anchor: dict[str, int]) -> None:
        self._trusted_time_anchor = dict(anchor)

    @staticmethod
    def _timer_fingerprint(timer: dict[str, Any]) -> tuple[Any, ...]:
        intent = timer.get("lastIntent")
        return (
            timer.get("id"),
            timer.get("status"),
            timer.get("phase"),
            timer.get("plannedDurationMs"),
            timer.get("anchorAt"),
            timer.get("elapsedAtAnchorMs"),
            timer.get("taskId"),
            intent.get("commandId") if isinstance(intent, dict) else None,
        )

    @staticmethod
    def _timer_continuity_identity(timer: dict[str, Any]) -> tuple[Any, ...]:
        intent = timer.get("lastIntent")
        return (
            timer.get("id"),
            timer.get("status"),
            timer.get("phase"),
            timer.get("plannedDurationMs"),
            timer.get("elapsedAtAnchorMs"),
            timer.get("taskId"),
            intent.get("commandId") if isinstance(intent, dict) else None,
        )

    def effective_timer_now_ms(
        self,
        timer: dict[str, Any] | None,
        *,
        physical_ms: int | None = None,
        monotonic_ms: int | None = None,
    ) -> int:
        physical_ms = self._physical_time_ms(
            int(time.time() * 1000) if physical_ms is None else physical_ms
        )
        if not timer or timer.get("status") != "running":
            self._timer_time_anchor = None
            return physical_ms
        monotonic_ms = self._bounded_integer(
            time.monotonic_ns() // 1_000_000 if monotonic_ms is None else monotonic_ms,
            "Monotonic clock",
        )
        identity = self._timer_continuity_identity(timer)
        timer_anchor = self._timer_time_anchor
        if (
            timer_anchor is None
            or timer_anchor["identity"] != identity
            or monotonic_ms < timer_anchor["monotonicMs"]
        ):
            timer_anchor = {
                "identity": identity,
                "elapsedMs": elapsed_ms(timer, physical_ms),
                "monotonicMs": monotonic_ms,
            }
            self._timer_time_anchor = timer_anchor
        projected_elapsed_ms = min(
            int(timer["plannedDurationMs"]),
            timer_anchor["elapsedMs"] + monotonic_ms - timer_anchor["monotonicMs"],
        )
        anchor_ms = parse_timestamp_ms(timer.get("anchorAt"))
        if anchor_ms is None:
            return physical_ms
        return self._physical_time_ms(
            anchor_ms + projected_elapsed_ms - int(timer.get("elapsedAtAnchorMs") or 0)
        )

    def _trusted_now_ms(
        self,
        physical_ms: int | None = None,
        *,
        use_server_clock: bool = True,
        use_monotonic: bool = True,
        sample: dict[str, int] | None = None,
    ) -> int:
        physical_ms = self._physical_time_ms(
            int(time.time() * 1000) if physical_ms is None else physical_ms
        )
        if not use_server_clock:
            return physical_ms
        sample = self._server_clock_sample(
            self.get_meta("serverClockSample") if sample is None else sample
        )
        if sample is None:
            return physical_ms
        if not use_monotonic:
            return self._physical_time_ms(physical_ms + sample["offsetMs"])
        monotonic_ms = self._bounded_integer(
            time.monotonic_ns() // 1_000_000, "Monotonic clock"
        )
        anchor = self._trusted_time_anchor
        if (
            anchor is None
            or anchor["offsetMs"] != sample["offsetMs"]
            or anchor["uncertaintyMs"] != sample["uncertaintyMs"]
            or anchor["acquiredPhysicalMs"] != sample["acquiredPhysicalMs"]
            or anchor["acquiredMonotonicMs"] != sample["acquiredMonotonicMs"]
            or anchor["acquiredTrustedMs"] != sample["acquiredTrustedMs"]
        ):
            projected_ms = self._projected_trusted_time(
                sample, physical_ms, monotonic_ms
            )
            if projected_ms is None:
                return physical_ms
            self._set_trusted_time_anchor(sample)
            return projected_ms
        if monotonic_ms < anchor["acquiredMonotonicMs"]:
            return physical_ms
        return self._physical_time_ms(
            anchor["acquiredTrustedMs"] + monotonic_ms - anchor["acquiredMonotonicMs"]
        )

    @classmethod
    def _logical_clock(
        cls, value: Any, *, allow_legacy_zero: bool = False
    ) -> tuple[int, int]:
        if not isinstance(value, dict):
            raise ValueError("Persisted logical clock is invalid.")
        wall_ms = cls._bounded_integer(value.get("wallMs"), "Logical clock wall time")
        counter = cls._bounded_integer(value.get("counter"), "Logical clock counter")
        if wall_ms == 0 and (not allow_legacy_zero or counter != 0):
            raise ValueError("Logical clock wall time must be positive.")
        return wall_ms, counter

    @classmethod
    def _operation_clock(
        cls,
        operation: dict[str, Any],
        *,
        allow_legacy_zero: bool = False,
    ) -> tuple[int, int, int]:
        occurred_at = operation.get("occurredAt")
        occurred_ms = (
            parse_timestamp_ms(occurred_at) if isinstance(occurred_at, str) else None
        )
        if occurred_ms is None:
            raise ValueError("Pending operation occurrence time is invalid.")
        wall_ms, counter = cls._logical_clock(
            {
                "wallMs": operation.get("hlcWallMs"),
                "counter": operation.get("hlcCounter"),
            },
            allow_legacy_zero=allow_legacy_zero,
        )
        if allow_legacy_zero and (wall_ms, counter) == (0, 0):
            if occurred_ms != 0:
                raise ValueError("Legacy pending operation clock is invalid.")
            return occurred_ms, wall_ms, counter
        cls._physical_time_ms(occurred_ms)
        if abs(wall_ms - occurred_ms) > MAX_CLOCK_SKEW_MS:
            raise ValueError("Pending operation clock exceeds the trusted-time limit.")
        return occurred_ms, wall_ms, counter

    def _reserve_generation(
        self,
        physical_now_ms: int,
        *,
        sequence_count: int = 0,
        clock_count: int = 1,
        use_server_clock: bool = True,
        use_monotonic: bool = False,
    ) -> tuple[int, list[int], list[tuple[int, int]]]:
        return self._generation_storage.reserve(
            physical_now_ms,
            sequence_count=sequence_count,
            clock_count=clock_count,
            use_server_clock=use_server_clock,
            use_monotonic=use_monotonic,
        )

    def _pending_uuid7_ids(self) -> list[str]:
        identifiers: list[str] = []
        rows = self.connection.execute(
            "SELECT id FROM pending_commands "
            "UNION ALL SELECT id FROM pending_task_operations "
            "UNION ALL SELECT id FROM pending_duration_operations "
            "UNION ALL SELECT id FROM pending_auto_start_operations "
            "UNION ALL SELECT id FROM pending_selected_task_operations"
        )
        for row in rows:
            identifier = str(row["id"])
            try:
                uuid7_parts(identifier)
            except ValueError:
                continue
            identifiers.append(identifier)
        return identifiers

    def _reserve_uuid7_ids(self, wall_ms: int, count: int) -> list[str]:
        stored = self.get_meta("lastUuidV7")
        if stored is not None:
            uuid7_parts(stored)

        pending = self._pending_uuid7_ids()
        latest_pending = max(
            pending, key=lambda value: uuid.UUID(value).int, default=None
        )
        if stored is None:
            previous = latest_pending
        else:
            previous = str(stored)
            if (
                latest_pending is not None
                and uuid.UUID(latest_pending).int > uuid.UUID(previous).int
            ):
                raise ValueError(
                    "Persisted UUIDv7 state predates a pending identifier."
                )

        identifiers = reserve_uuid7(wall_ms, count, previous)
        self._set_meta("lastUuidV7", identifiers[-1])
        return identifiers

    @classmethod
    def _canonical_durations(cls, durations_ms: Any) -> dict[str, int]:
        if not isinstance(durations_ms, dict) or set(durations_ms) != set(PHASES):
            raise ValueError("Server returned invalid duration preferences.")
        return {phase: cls._duration_ms(durations_ms[phase]) for phase in PHASES}

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def set_meta(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            self._set_meta(key, value)

    def _set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    @property
    def device_id(self) -> str:
        return str(self.get_meta("deviceId"))


    def load(self, *, projection: bool = False) -> dict[str, Any]:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            settings = self.get_meta("settings")
            snapshot = self.get_meta("snapshot")
            pending = self._pending_commands()
            pending_tasks = self._pending_operation_payloads(
                "pending_task_operations"
            )
            pending_durations = self._pending_durations_for_load()
            pending_auto_starts = self._pending_auto_start_operations()
            pending_selected_tasks = self._pending_selected_task_operations()
            pending_resolution = self._pending_resolution_for_load()
            state = {
                "settings": settings,
                "snapshot": snapshot,
                "pending": pending,
                "pendingTasks": pending_tasks,
                "pendingDurations": pending_durations,
                "pendingAutoStarts": pending_auto_starts,
                "pendingSelectedTasks": pending_selected_tasks,
                "pendingResolution": pending_resolution,
            }
            if projection:
                state["projectionSnapshot"] = self._physical_snapshot(snapshot)
                state["projectionPending"] = self._physical_pending_commands(pending)
        except BaseException:
            if owns_transaction:
                self.connection.rollback()
            raise
        if owns_transaction:
            self.connection.commit()
        return state

    def _pending_operation_payloads(self, table: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload"])
            for row in self.connection.execute(
                f"SELECT payload FROM {table} ORDER BY rowid"
            )
        ]

    def _pending_durations_for_load(self) -> list[dict[str, Any]]:
        operations = self._pending_operation_payloads("pending_duration_operations")
        operations.sort(
            key=lambda operation: (
                int(operation.get("hlcWallMs", 0)),
                int(operation.get("hlcCounter", 0)),
                str(operation.get("id", "")),
            )
        )
        return operations

    def _pending_resolution_for_load(self) -> Any:
        try:
            return self.get_meta("pendingResolution")
        except (TypeError, json.JSONDecodeError):
            return {"corrupted": True}

    def _pending_commands(self, *, sendable_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE depends_on_command_id IS NULL" if sendable_only else ""
        return [
            json.loads(row["payload"])
            for row in self.connection.execute(
                f"SELECT payload FROM pending_commands {where} ORDER BY device_sequence"
            )
        ]

    def _command_physical_times(self) -> dict[str, int]:
        value = self.get_meta("commandPhysicalTimes", {})
        if not isinstance(value, dict):
            raise ValueError("Persisted command physical times are invalid.")
        return {
            command_id: self._physical_time_ms(physical_ms)
            for command_id, physical_ms in value.items()
            if isinstance(command_id, str) and command_id
        }

    def _physical_pending_commands(
        self, commands: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        physical_times = self._command_physical_times()
        projected = []
        for command in commands:
            physical_ms = physical_times.get(str(command.get("id", "")))
            if physical_ms is None:
                projected.append(command)
                continue
            local_command = dict(command)
            local_command["occurredAt"] = utc_timestamp(physical_ms)
            projected.append(local_command)
        return projected

    def _physical_timestamp(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        sample = self._server_clock_sample(self.get_meta("serverClockSample"))
        if sample is None:
            return value
        trusted_ms = parse_timestamp_ms(value)
        if trusted_ms is None:
            return value
        physical_ms = trusted_ms - sample["offsetMs"]
        try:
            return utc_timestamp(self._physical_time_ms(physical_ms))
        except (OSError, OverflowError, ValueError):
            return value

    def _physical_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        projected = deepcopy(snapshot)
        timer = projected.get("canonicalTimer")
        if isinstance(timer, dict):
            timer["anchorAt"] = self._physical_timestamp(timer.get("anchorAt"))
            intent = timer.get("lastIntent")
            if isinstance(intent, dict):
                intent["occurredAt"] = self._physical_timestamp(
                    intent.get("occurredAt")
                )
        for item in projected.get("history", []):
            if not isinstance(item, dict):
                continue
            for key in ("completedAt", "endedAt"):
                if key in item:
                    item[key] = self._physical_timestamp(item.get(key))
        return projected

    def _record_command_physical_time(self, command_id: str, physical_ms: int) -> None:
        physical_times = self._command_physical_times()
        physical_times[command_id] = self._physical_time_ms(physical_ms)
        self._set_meta("commandPhysicalTimes", physical_times)

    def _prune_command_physical_times(self) -> None:
        pending_ids = {
            str(row["id"])
            for row in self.connection.execute("SELECT id FROM pending_commands")
        }
        physical_times = self._command_physical_times()
        retained = {
            command_id: physical_ms
            for command_id, physical_ms in physical_times.items()
            if command_id in pending_ids
        }
        if retained != physical_times:
            self._set_meta("commandPhysicalTimes", retained)

    def _pending_auto_start_operations(self) -> list[dict[str, Any]]:
        operations = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_auto_start_operations ORDER BY rowid"
            )
        ]
        operations.sort(
            key=lambda operation: (
                int(operation.get("hlcWallMs", 0)),
                int(operation.get("hlcCounter", 0)),
                str(operation.get("deviceId", "")),
                str(operation.get("id", "")),
            )
        )
        return operations

    def _pending_selected_task_operations(self) -> list[dict[str, Any]]:
        operations = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_selected_task_operations ORDER BY rowid"
            )
        ]
        operations.sort(
            key=lambda operation: (
                int(operation.get("hlcWallMs", 0)),
                int(operation.get("hlcCounter", 0)),
                str(operation.get("deviceId", "")),
                str(operation.get("id", "")),
            )
        )
        return operations

    @staticmethod
    def _pending_object(payload: Any, label: str) -> dict[str, Any]:
        try:
            operation = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Pending {label} contains invalid JSON.") from error
        if not isinstance(operation, dict):
            raise ValueError(f"Pending {label} must be an object.")
        return operation

    @staticmethod
    def _valid_identity(value: Any) -> bool:
        return isinstance(value, str) and bool(value)

    def _validate_pending_command(
        self, command: dict[str, Any], row: sqlite3.Row
    ) -> None:
        sequence = self._bounded_integer(
            command.get("deviceSequence"), "Pending timer command sequence", minimum=1
        )
        try:
            planned_ms = self._duration_ms(
                command.get("plannedDurationMs"),
                maximum=CANONICAL_DURATION_MAX_MS,
            )
        except ValueError as error:
            raise ValueError("Pending timer command duration is invalid.") from error
        observed_ms = command.get("observedElapsedMs")
        task_id = command.get("taskId")
        if (
            not self._valid_identity(command.get("id"))
            or command["id"] != row["id"]
            or sequence != row["device_sequence"]
            or not self._valid_identity(command.get("timerId"))
            or command.get("type") not in COMMAND_TYPES
            or command.get("phase") not in PHASES
            or isinstance(observed_ms, bool)
            or not isinstance(observed_ms, int)
            or not 0 <= observed_ms <= planned_ms
            or command.get("type") == "start"
            and observed_ms != 0
            or not isinstance(task_id, (str, type(None)))
            or isinstance(task_id, str)
            and not task_id
            or task_id is not None
            and (command.get("type") != "start" or command.get("phase") != "focus")
        ):
            raise ValueError("Pending timer command is invalid.")
        self._operation_clock(command)

    def _validate_pending_task_operation(
        self, operation: dict[str, Any], row: sqlite3.Row
    ) -> None:
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or not self._valid_identity(operation.get("taskId"))
            or operation.get("type") not in {"upsert", "delete"}
        ):
            raise ValueError("Pending task operation is invalid.")
        if operation["type"] == "upsert":
            title = operation.get("title")
            try:
                task = task_from_title(title) if isinstance(title, str) else None
            except ValueError:
                task = None
            if task is None or task["id"] != operation["taskId"]:
                raise ValueError("Pending task operation is invalid.")
        elif "title" in operation:
            raise ValueError("Pending task operation is invalid.")
        self._operation_clock(operation)

    def _validate_pending_duration_operation(
        self, operation: dict[str, Any], row: sqlite3.Row
    ) -> None:
        try:
            self._duration_ms(operation.get("durationMs"))
        except ValueError as error:
            raise ValueError("Pending duration operation is invalid.") from error
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("phase") not in PHASES
            or operation["phase"] != row["phase"]
        ):
            raise ValueError("Pending duration operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _validate_pending_auto_start_operation(
        self, operation: dict[str, Any], row: sqlite3.Row, device_id: str
    ) -> None:
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("deviceId") != device_id
            or not isinstance(operation.get("enabled"), bool)
        ):
            raise ValueError("Pending auto-start operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _validate_pending_selected_task_operation(
        self, operation: dict[str, Any], row: sqlite3.Row, device_id: str
    ) -> None:
        task_id = operation.get("taskId")
        if (
            not self._valid_identity(operation.get("id"))
            or operation["id"] != row["id"]
            or operation.get("deviceId") != device_id
            or not isinstance(task_id, (str, type(None)))
            or isinstance(task_id, str)
            and not task_id
        ):
            raise ValueError("Pending selected-task operation is invalid.")
        self._operation_clock(operation, allow_legacy_zero=True)

    def _preflight_pending_queues(
        self, *, require_clock_coverage: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        device_id = self.get_meta("deviceId")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("Persisted device identity is invalid.")
        persisted_sequence = self._bounded_integer(
            self.get_meta("deviceSequence", 0), "Persisted device sequence"
        )
        persisted_clock = self._logical_clock(
            self.get_meta("hlc", {"wallMs": 0, "counter": 0}),
            allow_legacy_zero=True,
        )
        self._server_clock_sample(self.get_meta("serverClockSample"))
        command_rows = self.connection.execute(
            "SELECT id, device_sequence, payload, depends_on_command_id "
            "FROM pending_commands ORDER BY device_sequence"
        ).fetchall()
        commands = self._validated_pending_rows(
            command_rows, "timer command", self._validate_pending_command
        )
        sendable_commands = [command for command, row in zip(commands, command_rows)
                             if row["depends_on_command_id"] is None]
        if commands and commands[-1]["deviceSequence"] > persisted_sequence:
            raise ValueError("Pending timer command exceeds persisted device sequence.")
        task_operations = self._preflight_operation_queue(
            "pending_task_operations", "task operation", self._validate_pending_task_operation,
            sort_by_clock=False)
        duration_operations = self._preflight_operation_queue(
            "pending_duration_operations", "duration operation",
            self._validate_pending_duration_operation, extra_column="phase")
        auto_start_operations = self._preflight_device_operation_queue(
            "pending_auto_start_operations", "auto-start operation",
            self._validate_pending_auto_start_operation, device_id)
        selected_task_operations = self._preflight_device_operation_queue(
            "pending_selected_task_operations", "selected-task operation",
            self._validate_pending_selected_task_operation, device_id)
        self._validate_pending_clock_coverage(
            (commands, task_operations, duration_operations, auto_start_operations,
             selected_task_operations), persisted_clock, require_clock_coverage)
        return {
            "commands": commands,
            "sendableCommands": sendable_commands,
            "taskOperations": task_operations,
            "durationOperations": duration_operations,
            "autoStartOperations": auto_start_operations,
            "selectedTaskOperations": selected_task_operations,
        }

    @staticmethod
    def _validate_pending_clock_coverage(
        queues: tuple[list[dict[str, Any]], ...], persisted_clock: tuple[int, int],
        required: bool,
    ) -> None:
        clocks = [(item["hlcWallMs"], item["hlcCounter"])
                  for queue in queues for item in queue]
        if required and clocks and max(clocks) > persisted_clock:
            raise ValueError("Pending operation exceeds persisted logical clock.")

    def _validated_pending_rows(self, rows: list[sqlite3.Row], label: str, validator: Any,
                                *validator_args: Any) -> list[dict[str, Any]]:
        operations = []
        for row in rows:
            operation = self._pending_object(row["payload"], label)
            validator(operation, row, *validator_args)
            operations.append(operation)
        return operations

    def _preflight_operation_queue(self, table: str, label: str, validator: Any,
                                   *, extra_column: str | None = None,
                                   sort_by_clock: bool = True) -> list[dict[str, Any]]:
        columns = f"id, {extra_column}, payload" if extra_column else "id, payload"
        rows = self.connection.execute(f"SELECT {columns} FROM {table} ORDER BY rowid").fetchall()
        operations = self._validated_pending_rows(rows, label, validator)
        if sort_by_clock:
            operations.sort(key=lambda item: (item["hlcWallMs"], item["hlcCounter"], item["id"]))
        return operations

    def _preflight_device_operation_queue(self, table: str, label: str, validator: Any,
                                          device_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(f"SELECT id, payload FROM {table} ORDER BY rowid").fetchall()
        operations = self._validated_pending_rows(rows, label, validator, device_id)
        return sorted(operations, key=lambda item: (
            item["hlcWallMs"], item["hlcCounter"], item["deviceId"], item["id"]))

    def save_settings(self, settings: dict[str, Any]) -> None:
        candidate = self._normalize_settings(settings)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            current = self._normalize_settings(self.get_meta("settings", {}))
            candidate["durations"] = current["durations"]
            candidate["durationsMs"] = current["durationsMs"]
            candidate["autoStartBreaks"] = current["autoStartBreaks"]
            candidate["selectedTaskId"] = current["selectedTaskId"]
            self._set_meta("settings", candidate)

    def _set_local_setting(self, key: str, value: Any) -> None:
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings[key] = value
            self._set_meta("settings", settings)
            self._capture_iroh_after_mutation_locked()

    def set_selected_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings["selectedPhase"] = phase
            self._set_meta("settings", settings)
            version = int(self.get_meta("selectedPhaseVersion", 0)) + 1
            self._set_meta("selectedPhaseVersion", version)
            self._capture_iroh_after_mutation_locked()

    def set_auto_start_breaks(
        self, enabled: bool, now_ms: int | None = None
    ) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("Auto-start preference must be true or false.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_auto_start_operation(
                enabled,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._set_meta("autoStartLegacyDefaultUnknown", False)
            self._capture_iroh_after_mutation_locked()
        return operation

    def set_selected_task_id(
        self, task_id: str | None, now_ms: int | None = None
    ) -> dict[str, Any] | None:
        if (
            not isinstance(task_id, (str, type(None)))
            or isinstance(task_id, str)
            and not task_id
        ):
            raise ValueError("Selected task identity must be non-empty or null.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_selected_task_operation(
                task_id,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._capture_iroh_after_mutation_locked()
        return operation

    def queue_command(
        self, command_type: str, timer: dict[str, Any] | None, selected_phase: str,
        durations_ms: dict[str, int], selected_task_id: str | None = None,
        now_ms: int | None = None, generate_auto_break: bool = False,
        automatic: bool = False,
    ) -> dict[str, Any] | None:
        use_server_clock = now_ms is None
        physical_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            return self._queue_command_transaction(
                command_type, timer, selected_phase, durations_ms, selected_task_id,
                physical_ms, generate_auto_break, automatic, use_server_clock)

    def _queue_command_transaction(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None,
        now_ms: int,
        generate_auto_break: bool = False,
        automatic: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any] | None:
        context = self._prepare_command_queue(
            command_type,
            timer,
            selected_task_id,
            now_ms,
            generate_auto_break,
            automatic,
            use_server_clock,
        )
        if context is None:
            return None
        command = self._queue_prepared_timer_command(
            command_type,
            context,
            selected_phase,
            durations_ms,
            now_ms,
            use_server_clock,
        )
        self._refresh_timer_after_queue(context.effective_now_ms, use_server_clock)
        return command

    def _prepare_command_queue(
        self, command_type: str, timer: dict[str, Any] | None,
        selected_task_id: str | None, now_ms: int, generate_auto_break: bool,
        automatic: bool, use_server_clock: bool,
    ) -> _CommandQueueContext | None:
        self._ensure_no_pending_resolution()
        effective_now_ms = (
            now_ms
            if not use_server_clock
            else self.effective_timer_now_ms(timer, physical_ms=now_ms)
        )
        state = self.load(projection=True)
        projection = self.projected_state(now_ms=effective_now_ms, state=state)
        settings = self.projected_settings(state, projection)
        policy = self._completion_policy.command_request(
            command_type, timer, projection.canonical_timer, automatic,
            generate_auto_break, settings["autoStartBreaks"],
        )
        if not policy.command_eligible:
            return None
        if automatic:
            timer = projection.canonical_timer
        if command_type == "start":
            selected_task_id = projection.selected_task_id
            if not any(
                task.get("id") == selected_task_id for task in projection.tasks
            ):
                selected_task_id = None
        return _CommandQueueContext(
            effective_now_ms, timer, selected_task_id,
            policy.reserve_generated_break,
        )


    def _queue_prepared_timer_command(
        self, command_type: str, context: _CommandQueueContext,
        selected_phase: str, durations_ms: dict[str, int], now_ms: int,
        use_server_clock: bool,
    ) -> dict[str, Any]:
        count = 2 if context.generates_break else 1
        trusted_ms, sequences, clocks = self._reserve_generation(
            context.effective_now_ms,
            sequence_count=count,
            clock_count=count,
            use_server_clock=use_server_clock,
            use_monotonic=use_server_clock,
        )
        command_ids = self._reserve_uuid7_ids(clocks[0][0], count)
        command = self._queue_command(
            command_type, context.timer, selected_phase, durations_ms,
            context.selected_task_id, now_ms,
            timer_now_ms=context.effective_now_ms, trusted_ms=trusted_ms,
            sequence=sequences[0], clock=clocks[0], command_id=command_ids[0],
        )
        if context.generates_break:
            self._queue_generated_auto_break(
                command, now_ms, timer_now_ms=context.effective_now_ms,
                trusted_ms=trusted_ms, sequence=sequences[1], clock=clocks[1],
                command_id=command_ids[1],
            )
        return command

    def _refresh_timer_after_queue(
        self, effective_now_ms: int, use_server_clock: bool
    ) -> None:
        state = self.load(projection=True)
        projected_timer = self.projected_state(
            now_ms=effective_now_ms, state=state
        ).canonical_timer
        if (
            use_server_clock
            and projected_timer
            and projected_timer.get("status") == "running"
        ):
            self.effective_timer_now_ms(
                projected_timer, physical_ms=effective_now_ms
            )
        self._capture_iroh_after_mutation_locked()

    def queue_restart(
        self,
        timer: dict[str, Any],
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            current_timer, selected_phase, durations_ms, selected_task_id = (
                self._terminal_action_context(
                    timer, now_ms, {"completed", "cancelled", "superseded"},
                    "Timer changed before restart could be saved.", validate_task=True))
            effective_now_ms = (
                now_ms
                if not use_server_clock
                else self.effective_timer_now_ms(current_timer, physical_ms=now_ms)
            )
            trusted_ms, sequences, clocks = self._reserve_generation(
                now_ms,
                sequence_count=2,
                clock_count=2,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            command_ids = self._reserve_uuid7_ids(clocks[0][0], 2)
            cleared, started = self._queue_reserved_pair(
                ("clear", current_timer), ("start", None), selected_phase,
                durations_ms, selected_task_id, now_ms, effective_now_ms, trusted_ms,
                sequences, clocks, command_ids)
            self._capture_iroh_after_mutation_locked()
        return [cleared, started]

    def queue_cancel_and_clear(
        self,
        timer: dict[str, Any],
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None = None,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            current_timer, selected_phase, durations_ms, selected_task_id = (
                self._terminal_action_context(
                    timer, now_ms, ACTIVE_STATUSES,
                    "Timer changed before cancel could be saved."))
            effective_now_ms = (
                now_ms
                if not use_server_clock
                else self.effective_timer_now_ms(current_timer, physical_ms=now_ms)
            )
            trusted_ms, sequences, clocks = self._reserve_generation(
                effective_now_ms,
                sequence_count=2,
                clock_count=2,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            command_ids = self._reserve_uuid7_ids(clocks[0][0], 2)
            cancelled, cleared = self._queue_reserved_pair(
                ("cancel", current_timer), ("clear", current_timer), selected_phase,
                durations_ms, selected_task_id, now_ms, effective_now_ms, trusted_ms,
                sequences, clocks, command_ids)
            self._capture_iroh_after_mutation_locked()
        return [cancelled, cleared]

    def _terminal_action_context(self, timer: dict[str, Any], now_ms: int,
                                 statuses: set[str], error: str, *, validate_task: bool = False
                                 ) -> tuple[dict[str, Any], str, dict[str, int], str | None]:
        state = self.load(projection=True)
        projection = self.projected_state(now_ms=now_ms, state=state)
        current = projection.canonical_timer
        if (not isinstance(current, dict) or current.get("status") not in statuses
                or self._timer_fingerprint(current) != self._timer_fingerprint(timer)):
            raise ValueError(error)
        settings = self.projected_settings(state, projection)
        task_id = settings.get("selectedTaskId")
        if validate_task and not any(task.get("id") == task_id for task in projection.tasks):
            task_id = None
        return current, settings["selectedPhase"], settings["durationsMs"], task_id

    def _queue_reserved_pair(self, first: tuple[str, dict[str, Any] | None],
                             second: tuple[str, dict[str, Any] | None], phase: str,
                             durations: dict[str, int], task_id: str | None, now_ms: int,
                             timer_now_ms: int, trusted_ms: int, sequences: list[int],
                             clocks: list[tuple[int, int]], ids: list[str]
                             ) -> tuple[dict[str, Any], dict[str, Any]]:
        commands = [self._queue_command(
            kind, timer, phase, durations, task_id, now_ms, timer_now_ms=timer_now_ms,
            trusted_ms=trusted_ms, sequence=sequences[index], clock=clocks[index],
            command_id=ids[index])
            for index, (kind, timer) in enumerate((first, second))]
        return commands[0], commands[1]

    def _queue_command(
        self, command_type: str, timer: dict[str, Any] | None, selected_phase: str,
        durations_ms: dict[str, int], selected_task_id: str | None, now_ms: int,
        depends_on_command_id: str | None = None, *, timer_now_ms: int | None = None,
        trusted_ms: int | None = None, sequence: int | None = None,
        clock: tuple[int, int] | None = None, command_id: str | None = None,
    ) -> dict[str, Any]:
        generation = self._command_generation(
            now_ms, timer_now_ms, trusted_ms, sequence, clock, command_id)
        return self._persist_timer_command(
            command_type, timer, selected_phase, durations_ms, selected_task_id,
            now_ms, depends_on_command_id, *generation)

    def _persist_timer_command(
        self,
        command_type: str,
        timer: dict[str, Any] | None,
        selected_phase: str,
        durations_ms: dict[str, int],
        selected_task_id: str | None,
        now_ms: int,
        depends_on_command_id: str | None,
        timer_now_ms: int, trusted_ms: int, sequence: int,
        clock: tuple[int, int], command_id: str,
    ) -> dict[str, Any]:
        command, depends_on_command_id = self._prepare_timer_command(
            command_type, timer, selected_phase, durations_ms, selected_task_id,
            timer_now_ms, trusted_ms, sequence, clock, command_id,
            depends_on_command_id,
        )
        projection_settings = self._normalize_settings(self.get_meta("settings", {}))
        self._project_timer_command(command, projection_settings)
        self.connection.execute(
            "INSERT INTO pending_commands("
            "id, device_sequence, payload, depends_on_command_id) VALUES (?, ?, ?, ?)",
            (
                command["id"],
                sequence,
                json.dumps(command, separators=(",", ":")),
                depends_on_command_id,
            ),
        )
        self._record_command_physical_time(command["id"], timer_now_ms)
        self._apply_timer_command_side_effects(command, timer)
        self._set_meta("deviceSequence", sequence)
        self._set_meta(
            "hlc", {"wallMs": command["hlcWallMs"], "counter": command["hlcCounter"]}
        )
        return command

    def _prepare_timer_command(
        self, command_type: str, timer: dict[str, Any] | None,
        selected_phase: str, durations_ms: dict[str, int],
        selected_task_id: str | None, timer_now_ms: int, trusted_ms: int,
        sequence: int, clock: tuple[int, int], command_id: str,
        dependency: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        if command_type not in COMMAND_TYPES:
            raise ValueError("Unsupported timer command.")
        wall_ms, counter = clock
        starting = command_type == "start"
        if starting:
            phase = selected_phase if selected_phase in PHASES else "focus"
            timer_id = str(uuid.uuid4())
            planned_ms = self._duration_ms(durations_ms[phase])
            observed_ms = 0
        else:
            if not timer or not timer.get("id"):
                raise ValueError("No timer is available for this action.")
            phase = str(timer["phase"])
            timer_id = str(timer["id"])
            planned_ms = int(timer["plannedDurationMs"])
            observed_ms = round(elapsed_ms(timer, timer_now_ms))
            if dependency is None:
                for row in self.connection.execute(
                    "SELECT depends_on_command_id, payload FROM pending_commands "
                    "WHERE depends_on_command_id IS NOT NULL"
                ):
                    pending_command = json.loads(row["payload"])
                    if pending_command.get("timerId") == timer_id:
                        dependency = str(row["depends_on_command_id"])
                        break
        command = {
            "id": command_id,
            "deviceSequence": sequence,
            "timerId": timer_id,
            "type": command_type,
            "phase": phase,
            "plannedDurationMs": planned_ms,
            "occurredAt": utc_timestamp(trusted_ms),
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
            "observedElapsedMs": observed_ms,
        }
        if starting and phase == "focus" and selected_task_id:
            command["taskId"] = selected_task_id
        return command, dependency

    def _apply_timer_command_side_effects(
        self, command: dict[str, Any], timer: dict[str, Any] | None
    ) -> None:
        if command["type"] == "start" and self.replication_mode != "iroh":
            self._set_meta(
                "centralizedTimerOwnership",
                {
                    "timerId": command["timerId"],
                    "deviceId": self.device_id,
                    "startCommandId": command["id"],
                },
            )
        if command["type"] == "finish":
            self._apply_finish_command(command, timer)

    def _apply_finish_command(
        self, command: dict[str, Any], timer: dict[str, Any] | None
    ) -> None:
        settings = self._normalize_settings(self.get_meta("settings", {}))
        projection = self._project_operation(
            settings, now=command["occurredAt"]
        )
        policy = self._completion_policy.finish_applied(command, timer, projection)
        advanced_phase = policy.selected_phase
        if advanced_phase is None:
            raise ValueError("SharedCore omitted completed timer phase advance.")
        settings["selectedPhase"] = advanced_phase
        self._set_meta("settings", settings)
        self.connection.execute(
            "INSERT INTO pending_phase_advances("
            "finish_command_id, timer_id, source_phase, advanced_phase, "
            "selected_phase_version) VALUES (?, ?, ?, ?, ?)",
            (
                command["id"], command["timerId"], command["phase"], advanced_phase,
                int(self.get_meta("selectedPhaseVersion", 0)),
            ),
        )
        if policy.queue_auto_break:
            self.connection.execute(
                "INSERT OR IGNORE INTO pending_auto_breaks("
                "finish_command_id, timer_id, finish_device_sequence) "
                "VALUES (?, ?, ?)",
                (command["id"], command["timerId"], command["deviceSequence"]),
            )


    def _command_generation(self, now_ms: int, timer_now_ms: int | None,
                            trusted_ms: int | None, sequence: int | None,
                            clock: tuple[int, int] | None, command_id: str | None
                            ) -> tuple[int, int, int, tuple[int, int], str]:
        if sequence is None or clock is None:
            trusted_ms, sequences, clocks = self._reserve_generation(
                now_ms, sequence_count=1, clock_count=1)
            sequence, clock = sequences[0], clocks[0]
        if trusted_ms is None:
            trusted_ms = self._trusted_now_ms(now_ms, use_monotonic=False)
        timer_now_ms = now_ms if timer_now_ms is None else timer_now_ms
        if command_id is None:
            command_id = self._reserve_uuid7_ids(clock[0], 1)[0]
        return timer_now_ms, trusted_ms, sequence, clock, command_id

    def _queue_generated_auto_break(
        self,
        finish: dict[str, Any],
        now_ms: int,
        *,
        timer_now_ms: int,
        trusted_ms: int,
        sequence: int,
        clock: tuple[int, int],
        command_id: str,
    ) -> dict[str, Any]:
        settings = self._normalize_settings(self.get_meta("settings", {}))
        state = self.load(projection=True)
        snapshot = state.get("projectionSnapshot", state["snapshot"])
        optimistic = self._project_operation(
            settings, now=finish["occurredAt"], base=snapshot, state=state,
        )
        policy = self._completion_policy.generated_break(
            {"commandId": finish["id"], "timerId": finish["timerId"]},
            snapshot, optimistic, True, False, finish["occurredAt"],
        )
        phase = policy.generated_break_phase
        if not policy.generated_break_eligible or phase is None:
            raise ValueError(
                "Automatic break generation requires an accepted focus finish."
            )
        settings["selectedPhase"] = phase
        self._set_meta("settings", settings)
        self.connection.execute(
            "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
            (finish["id"],),
        )
        command = self._queue_command(
            "start",
            None,
            phase,
            settings["durationsMs"],
            None,
            now_ms,
            finish["id"],
            timer_now_ms=timer_now_ms,
            trusted_ms=trusted_ms,
            sequence=sequence,
            clock=clock,
            command_id=command_id,
        )
        self._record_auto_break_start(finish["id"], finish["timerId"], command["id"])
        return command


    def _record_auto_break_start(self, finish_id: str, timer_id: str, start_id: str) -> None:
        self.connection.execute(
            "INSERT INTO pending_auto_break_starts("
            "source_finish_command_id, source_timer_id, start_command_id, "
            "selected_phase_version) VALUES (?, ?, ?, ?)",
            (finish_id, timer_id, start_id, int(self.get_meta("selectedPhaseVersion", 0))))

    def queue_task_operation(
        self,
        operation_type: str,
        task: dict[str, str],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if operation_type not in {"upsert", "delete"}:
            raise ValueError("Unsupported task operation.")
        normalized = self._normalized_task_identity(task)
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            operation = self._create_task_operation(
                operation_type, normalized, now_ms, use_server_clock)
            settings = self._normalize_settings(self.get_meta("settings", {}))
            self._project_task_operation(operation, settings)
            self.connection.execute(
                "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
                (operation["id"], json.dumps(operation, separators=(",", ":"))),
            )
            self._remember_known_task(normalized)
            self._set_meta("hlc", {"wallMs": operation["hlcWallMs"],
                                   "counter": operation["hlcCounter"]})
            self._capture_iroh_after_mutation_locked()
        return operation

    def _normalized_task_identity(self, task: dict[str, str]) -> dict[str, str]:
        core = (
            self._shared_core
            if self._shared_core is not None
            else _default_shared_core()
        )
        try:
            normalized_value = core.dispatch(
                "task.identity.v1", {"title": task.get("title", "")}
            )
        except SharedCoreError as error:
            raise ValueError(str(error)) from error
        if not isinstance(normalized_value, dict):
            raise ValueError("Shared core returned an invalid task identity.")
        task_id = normalized_value.get("id")
        title = normalized_value.get("title")
        utf8_bytes = normalized_value.get("utf8Bytes")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(title, str)
            or not title
            or isinstance(utf8_bytes, bool)
            or not isinstance(utf8_bytes, int)
            or utf8_bytes != len(title.encode("utf-8"))
            or utf8_bytes > 512
        ):
            raise ValueError("Shared core returned an invalid task identity.")
        normalized = {"id": task_id, "title": title}
        if normalized["id"] != task.get("id"):
            raise ValueError("Task identity does not match its name.")
        return normalized

    def _create_task_operation(self, operation_type: str, task: dict[str, str],
                               now_ms: int, use_server_clock: bool) -> dict[str, Any]:
        trusted_ms, _sequences, clocks = self._reserve_generation(
            now_ms, use_server_clock=use_server_clock, use_monotonic=use_server_clock)
        wall_ms, counter = clocks[0]
        operation = {"id": self._reserve_uuid7_ids(wall_ms, 1)[0], "taskId": task["id"],
                     "type": operation_type, "occurredAt": utc_timestamp(trusted_ms),
                     "hlcWallMs": wall_ms, "hlcCounter": counter}
        if operation_type == "upsert":
            operation["title"] = task["title"]
        return operation

    def _remember_known_task(self, normalized: dict[str, str]) -> None:
        snapshot = self.get_meta("snapshot", {})
        known = {
                item["id"]: item
                for item in snapshot.get("knownTasks", [])
                if item.get("id") and item.get("title")
            }
        known[normalized["id"]] = normalized
        snapshot["knownTasks"] = sorted(
            known.values(), key=lambda item: (item["title"].encode(), item["id"].encode()))
        self._set_meta("snapshot", snapshot)

    def queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValueError("Unsupported timer phase.")
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            self._ensure_no_pending_resolution()
            settings = self._normalize_settings(self.get_meta("settings", {}))
            operation = self._queue_duration_operation(
                phase,
                duration_ms,
                settings,
                now_ms,
                use_server_clock=use_server_clock,
            )
            self._capture_iroh_after_mutation_locked()
        return operation

    def _project_operation(
        self,
        settings: dict[str, Any],
        *,
        duration_operation: dict[str, Any] | None = None,
        auto_start_operation: dict[str, Any] | None = None,
        selected_task_operation: dict[str, Any] | None = None,
        command_operation: dict[str, Any] | None = None,
        task_operation: dict[str, Any] | None = None,
        now: str | None = None,
        base: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        pending_commands: list[dict[str, Any]] | None = None,
    ) -> ProjectionApplyV2:
        state = self.load() if state is None else state
        snapshot = state["snapshot"]
        projection_base = snapshot if base is None else base
        pending = self._projection_pending(
            state, duration_operation, auto_start_operation, selected_task_operation,
            command_operation, task_operation, pending_commands)
        prospective = (
            duration_operation
            or auto_start_operation
            or selected_task_operation
            or command_operation
            or task_operation
        )
        if prospective is None and now is None:
            raise ValueError(
                "Shared-core projection requires a prospective operation or projection time."
            )
        projection_now = prospective["occurredAt"] if prospective is not None else now
        projection_input = self._projection_input(
            projection_base, settings, pending, projection_now)
        core = (
            self._shared_core
            if self._shared_core is not None
            else _default_shared_core()
        )
        try:
            return apply_projection_v2(core, projection_input)
        except SharedCoreError as error:
            raise ValueError(str(error)) from error

    def _with_device_id(self, item: dict[str, Any]) -> dict[str, Any]:
        projected = dict(item)
        projected.setdefault("deviceId", self.device_id)
        return projected

    def _projection_pending(self, state: dict[str, Any], duration: dict[str, Any] | None,
                            auto_start: dict[str, Any] | None, selected_task: dict[str, Any] | None,
                            command: dict[str, Any] | None, task: dict[str, Any] | None,
                            commands: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
        durations = [self._with_device_id(item) for item in state["pendingDurations"]
                     if duration is None or item.get("phase") != duration["phase"]]
        sources = {"commands": state["pending"] if commands is None else commands,
                   "taskOperations": state["pendingTasks"],
                   "autoStartOperations": state["pendingAutoStarts"],
                   "selectedTaskOperations": state["pendingSelectedTasks"]}
        pending = {key: [self._with_device_id(item) for item in value]
                   for key, value in sources.items()}
        pending["durationOperations"] = durations
        for key, operation in (("durationOperations", duration), ("autoStartOperations", auto_start),
                               ("selectedTaskOperations", selected_task), ("commands", command),
                               ("taskOperations", task)):
            if operation is not None:
                pending[key].append(self._with_device_id(operation))
        return pending

    @staticmethod
    def _projection_input(base: dict[str, Any], settings: dict[str, Any],
                          pending: dict[str, list[dict[str, Any]]], now: str | None
                          ) -> dict[str, Any]:
        history = base.get("history", [])
        timer = base.get("canonicalTimer")
        if isinstance(timer, dict) and any(
                isinstance(item, dict) and item.get("timerId") == timer.get("id")
                for item in history):
            timer = None
        return {"base": {"canonicalTimer": timer, "history": history,
                         "tasks": base.get("tasks", []), "durationsMs": settings["durationsMs"],
                         "autoStartBreaks": bool(base.get("autoStartBreaks", False)),
                         "selectedTaskId": base.get("selectedTaskId")},
                "pending": pending, "now": now}

    def projected_state(
        self,
        *,
        now_ms: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> ProjectionApplyV2:
        """Return fail-closed synchronized state from production SharedCore."""
        state = self.load(projection=True) if state is None else state
        projection_base = state.get("projectionSnapshot", state["snapshot"])
        pending_commands = state.get("projectionPending", state["pending"])
        projection_now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return self._project_operation(
            self._normalize_settings(state["settings"]),
            now=utc_timestamp(projection_now_ms),
            base=projection_base,
            state=state,
            pending_commands=pending_commands,
        )

    def projected_settings(
        self,
        state: dict[str, Any],
        projection: ProjectionApplyV2,
    ) -> dict[str, Any]:
        settings = self._normalize_settings(state["settings"])
        settings["durationsMs"] = dict(projection.durations_ms)
        settings["durations"] = {
            phase: self._display_minutes(duration_ms)
            for phase, duration_ms in projection.durations_ms.items()
        }
        settings["autoStartBreaks"] = projection.auto_start_breaks
        settings["selectedTaskId"] = projection.selected_task_id
        return settings

    @staticmethod
    def projected_history(
        projection: ProjectionApplyV2,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Annotate core history with local-only pending-sync presentation state."""
        pending_timer_ids = {
            command.get("timerId")
            for command in state["pending"]
            if isinstance(command.get("timerId"), str)
        }
        return [
            {**item, "pending": True}
            if item.get("timerId") in pending_timer_ids
            else item
            for item in projection.history
        ]

    def _validated_projection_state(
        self,
        projection: dict[str, Any],
        *,
        context: str,
    ) -> tuple[
        dict[str, Any] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, int],
        bool,
        str | None,
    ]:
        timer = projection.get("canonicalTimer")
        history = projection.get("history")
        tasks = projection.get("tasks")
        durations = projection.get("durationsMs")
        auto_start = projection.get("autoStartBreaks")
        selected_task_id = projection.get("selectedTaskId")
        if timer is not None and not isinstance(timer, dict):
            raise ValueError(
                f"Shared core returned an invalid {context} timer projection."
            )
        history = self._validated_projection_items(
            history, f"Shared core returned an invalid {context} history projection."
        )
        tasks = self._validated_projection_items(
            tasks, f"Shared core returned an invalid {context} task projection."
        )
        canonical_durations = self._canonical_durations(durations)
        if not isinstance(auto_start, bool):
            raise ValueError(
                f"Shared core returned an invalid {context} auto-start projection."
            )
        if selected_task_id is not None and not isinstance(selected_task_id, str):
            raise ValueError(
                f"Shared core returned an invalid {context} selection projection."
            )
        if selected_task_id is not None and not any(
            task.get("id") == selected_task_id for task in tasks
        ):
            raise ValueError(f"Shared core selected an unavailable {context} task.")
        return (
            timer,
            history,
            tasks,
            canonical_durations,
            auto_start,
            selected_task_id,
        )

    @staticmethod
    def _validated_projection_items(value: Any, error: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError(error)
        return value

    def _project_duration_operation(
        self,
        operation: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, int]:
        value = self._project_operation(settings, duration_operation=operation)
        durations = value.durations_ms
        duration_winners = value.winning_operation_ids.durations
        if (
            duration_winners.get(operation["phase"]) != operation["id"]
            or durations[operation["phase"]] != operation["durationMs"]
        ):
            raise ValueError("Shared core returned an invalid duration projection.")
        return durations

    def _project_auto_start_operation(
        self,
        operation: dict[str, Any],
        settings: dict[str, Any],
    ) -> bool:
        value = self._project_operation(settings, auto_start_operation=operation)
        enabled = value.auto_start_breaks
        winner = value.winning_operation_ids.auto_start
        if (
            not isinstance(enabled, bool)
            or enabled != operation["enabled"]
            or winner != operation["id"]
        ):
            raise ValueError("Shared core returned an invalid auto-start projection.")
        return enabled

    def _project_selected_task_operation(
        self,
        operation: dict[str, Any],
        settings: dict[str, Any],
    ) -> str | None:
        value = self._project_operation(settings, selected_task_operation=operation)
        selected_task_id = value.selected_task_id
        winner = value.winning_operation_ids.selected_task
        if selected_task_id != operation["taskId"] or winner != operation["id"]:
            raise ValueError(
                "Shared core returned an invalid selected-task projection."
            )
        return selected_task_id

    def _project_timer_command(
        self,
        command: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        value = self._project_operation(settings, command_operation=command)
        outcome = value.timer_outcomes.get(command["id"])
        if outcome is None or outcome["outcome"] != "applied":
            raise ValueError("Shared core rejected the timer command projection.")

    def _project_task_operation(
        self,
        operation: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        value = self._project_operation(settings, task_operation=operation)
        tasks = value.tasks
        task_winners = value.winning_operation_ids.tasks
        projected = next(
            (
                item
                for item in tasks
                if isinstance(item, dict) and item.get("id") == operation["taskId"]
            ),
            None,
        )
        expected = (
            {"id": operation["taskId"], "title": operation["title"]}
            if operation["type"] == "upsert"
            else None
        )
        if (
            task_winners.get(operation["taskId"]) != operation["id"]
            or projected != expected
        ):
            raise ValueError("Shared core returned an invalid task projection.")

    def _queue_duration_operation(
        self,
        phase: str,
        duration_ms: int,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        operation = self._new_duration_operation(
            phase, duration_ms, now_ms, bootstrap, use_server_clock
        )
        self._persist_duration_operation(operation, settings, bootstrap)
        return operation

    def _new_duration_operation(
        self, phase: str, duration_ms: int, now_ms: int,
        bootstrap: bool, use_server_clock: bool,
    ) -> dict[str, Any]:
        duration_ms = self._duration_ms(duration_ms)
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "phase": phase,
            "durationMs": duration_ms,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        return operation

    def _persist_duration_operation(
        self, operation: dict[str, Any], settings: dict[str, Any], bootstrap: bool
    ) -> None:
        projected_durations = self._project_duration_operation(operation, settings)
        self.connection.execute(
            "INSERT INTO pending_duration_operations(id, phase, payload) "
            "VALUES (?, ?, ?) ON CONFLICT(phase) DO UPDATE SET "
            "id = excluded.id, payload = excluded.payload",
            (
                operation["id"],
                operation["phase"],
                json.dumps(operation, separators=(",", ":")),
            ),
        )
        settings["durationsMs"] = projected_durations
        settings["durations"] = {
            item_phase: self._display_minutes(item_duration)
            for item_phase, item_duration in projected_durations.items()
        }
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta(
                "hlc",
                {
                    "wallMs": operation["hlcWallMs"],
                    "counter": operation["hlcCounter"],
                },
            )

    def _queue_auto_start_operation(
        self,
        enabled: bool,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "deviceId": self.device_id,
            "enabled": enabled,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        projected_enabled = self._project_auto_start_operation(operation, settings)
        self.connection.execute(
            "INSERT INTO pending_auto_start_operations(id, payload) VALUES (?, ?)",
            (operation["id"], json.dumps(operation, separators=(",", ":"))),
        )
        settings["autoStartBreaks"] = projected_enabled
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    def _queue_selected_task_operation(
        self,
        task_id: str | None,
        settings: dict[str, Any],
        now_ms: int,
        bootstrap: bool = False,
        use_server_clock: bool = False,
    ) -> dict[str, Any]:
        if bootstrap:
            occurred_at = utc_timestamp(0)
            wall_ms = 0
            counter = 0
            operation_id = str(uuid.uuid4())
        else:
            occurred_ms, _sequences, clocks = self._reserve_generation(
                now_ms,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            wall_ms, counter = clocks[0]
            occurred_at = utc_timestamp(occurred_ms)
            operation_id = self._reserve_uuid7_ids(wall_ms, 1)[0]
        operation = {
            "id": operation_id,
            "deviceId": self.device_id,
            "taskId": task_id,
            "occurredAt": occurred_at,
            "hlcWallMs": wall_ms,
            "hlcCounter": counter,
        }
        projected_task_id = self._project_selected_task_operation(operation, settings)
        self.connection.execute(
            "INSERT INTO pending_selected_task_operations(id, payload) VALUES (?, ?)",
            (operation["id"], json.dumps(operation, separators=(",", ":"))),
        )
        settings["selectedTaskId"] = projected_task_id
        self._set_meta("settings", settings)
        if not bootstrap:
            self._set_meta("hlc", {"wallMs": wall_ms, "counter": counter})
        return operation

    def set_user(self, user: dict[str, Any] | None) -> None:
        with self._immediate_transaction():
            snapshot = self.get_meta("snapshot")
            snapshot["user"] = user
            self._set_meta("snapshot", snapshot)

    def provisional_auto_break_timer_ids(self) -> set[str]:
        timer_ids: set[str] = set()
        for row in self.connection.execute(
            "SELECT commands.payload FROM pending_auto_break_starts AS starts "
            "JOIN pending_commands AS commands ON commands.id = starts.start_command_id"
        ):
            timer_id = json.loads(row["payload"]).get("timerId")
            if isinstance(timer_id, str) and timer_id:
                timer_ids.add(timer_id)
        return timer_ids

    def load_with_provisional_auto_breaks(
        self,
    ) -> tuple[dict[str, Any], set[str]]:
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        try:
            state = self.load(projection=True)
            timer_ids = self.provisional_auto_break_timer_ids()
        except BaseException:
            if owns_transaction:
                self.connection.rollback()
            raise
        if owns_transaction:
            self.connection.commit()
        return state, timer_ids

    def process_auto_break(
        self, *, require_canonical: bool, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        use_server_clock = now_ms is None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._immediate_transaction():
            if self.pending_resolution() is not None:
                return []
            if self.has_pending_auto_break():
                self._reserve_generation(
                    now_ms,
                    clock_count=0,
                    use_server_clock=use_server_clock,
                    use_monotonic=use_server_clock,
                )
            return self._process_auto_break_queue(
                require_canonical, now_ms, use_server_clock)

    def _process_auto_break_queue(
        self, require_canonical: bool, now_ms: int, use_server_clock: bool
    ) -> list[dict[str, Any]]:
        while True:
            trigger = self._next_auto_break_trigger()
            if trigger is None:
                return []
            pending = self._pending_commands_for_auto_break()
            if self._auto_break_has_later_command(trigger, pending):
                self._discard_auto_break(trigger.timer_id)
                continue
            source_finish_pending = any(
                command.get("id") == trigger.finish_command_id for command in pending
            )
            if require_canonical and source_finish_pending:
                return []
            snapshot = self.get_meta("snapshot", {})
            if require_canonical and self._pending_auto_start_operations():
                return []
            context = self._auto_break_context(
                trigger, pending, snapshot, source_finish_pending,
                require_canonical, now_ms,
            )
            if context is None:
                return []
            trusted_ms, sequences, clocks = self._reserve_generation(
                now_ms, sequence_count=1, clock_count=1,
                use_server_clock=use_server_clock,
                use_monotonic=use_server_clock,
            )
            self._discard_auto_break(trigger.timer_id)
            if context.generated_phase is None:
                continue
            command = self._queue_auto_break_start(
                trigger, context, require_canonical, now_ms,
                trusted_ms, sequences[0], clocks[0],
            )
            return [command]

    def _next_auto_break_trigger(self) -> _AutoBreakTrigger | None:
        row = self.connection.execute(
            "SELECT finish_command_id, timer_id, finish_device_sequence "
            "FROM pending_auto_breaks ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _AutoBreakTrigger(
            str(row["finish_command_id"]),
            str(row["timer_id"]),
            int(row["finish_device_sequence"]),
        )

    def _pending_commands_for_auto_break(self) -> list[dict[str, Any]]:
        commands = [
            json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT payload FROM pending_commands ORDER BY device_sequence"
            )
        ]
        return self._physical_pending_commands(commands)

    @staticmethod
    def _auto_break_has_later_command(
        trigger: _AutoBreakTrigger, pending: list[dict[str, Any]]
    ) -> bool:
        return any(
            int(command.get("deviceSequence", 0)) > trigger.finish_sequence
            and not (
                command.get("type") == "finish"
                and command.get("timerId") == trigger.timer_id
            )
            for command in pending
        )

    def _discard_auto_break(self, timer_id: str) -> None:
        self.connection.execute(
            "DELETE FROM pending_auto_breaks WHERE timer_id = ?", (timer_id,)
        )

    def _auto_break_context(
        self, trigger: _AutoBreakTrigger, pending: list[dict[str, Any]],
        snapshot: dict[str, Any], source_finish_pending: bool,
        require_canonical: bool, now_ms: int,
    ) -> _AutoBreakContext | None:
        base_history = snapshot.get("history", [])
        settings = self._normalize_settings(self.get_meta("settings", {}))
        state = self.load(projection=True)
        optimistic = self._project_operation(
            settings, now=utc_timestamp(now_ms), base=snapshot, state=state,
            pending_commands=pending,
        )
        source_timestamp = self._completion_source_timestamp(
            trigger, pending, base_history, optimistic.history, now_ms
        )
        policy = self._completion_policy.generated_break(
            {
                "commandId": trigger.finish_command_id,
                "timerId": trigger.timer_id,
            },
            snapshot, optimistic, source_finish_pending,
            require_canonical, source_timestamp,
        )
        if not policy.generated_break_eligible:
            return None
        return _AutoBreakContext(
            settings, policy.generated_break_phase,
            policy.source_already_accepted,
        )

    @staticmethod
    def _completion_source_timestamp(
        trigger: _AutoBreakTrigger, pending: list[dict[str, Any]],
        canonical_history: list[dict[str, Any]],
        optimistic_history: list[dict[str, Any]], now_ms: int,
    ) -> str:
        candidates = [*pending, *canonical_history, *optimistic_history]
        source = next((item for item in candidates
                       if item.get("timerId") == trigger.timer_id
                       and (item.get("id") == trigger.finish_command_id
                            or item.get("commandId") == trigger.finish_command_id)), None)
        if source is None:
            return utc_timestamp(now_ms)
        return str(source.get("completedAt") or source.get("endedAt")
                   or source.get("occurredAt") or utc_timestamp(now_ms))

    def _queue_auto_break_start(
        self, trigger: _AutoBreakTrigger, context: _AutoBreakContext,
        require_canonical: bool, now_ms: int, trusted_ms: int,
        sequence: int, clock: tuple[int, int],
    ) -> dict[str, Any]:
        phase = context.generated_phase
        if phase is None:
            raise ValueError("Automatic break completion is missing.")
        context.settings["selectedPhase"] = phase
        self._set_meta("settings", context.settings)
        command_id = self._reserve_uuid7_ids(clock[0], 1)[0]
        provisional = not require_canonical and not context.source_already_accepted
        command = self._queue_command(
            "start", None, phase, context.settings["durationsMs"], None, now_ms,
            trigger.finish_command_id if provisional else None,
            trusted_ms=trusted_ms, sequence=sequence, clock=clock,
            command_id=command_id,
        )
        if provisional:
            self.connection.execute(
                "INSERT INTO pending_auto_break_starts("
                "source_finish_command_id, source_timer_id, start_command_id, "
                "selected_phase_version) VALUES (?, ?, ?, ?)",
                (
                    trigger.finish_command_id, trigger.timer_id, command["id"],
                    int(self.get_meta("selectedPhaseVersion", 0)),
                ),
            )
        return command

    def reset_account_data(self) -> None:
        with self._immediate_transaction():
            room_id = self.active_iroh_room_id
            if self.replication_mode == "iroh" and room_id is not None:
                self._reset_iroh_account_data(room_id)
                return
            self._clear_account_queues()
            self._set_meta("commandPhysicalTimes", {})
            self._set_meta("centralizedTimerOwnership", None)
            self._set_meta("pendingSync", None)
            self._set_meta("pendingResolution", None)
            settings = self._normalize_settings(self.get_meta("settings", {}))
            settings["durations"] = {
                phase: int(definition["default_minutes"])
                for phase, definition in PHASES.items()
            }
            settings["durationsMs"] = {
                phase: int(definition["default_minutes"]) * 60_000
                for phase, definition in PHASES.items()
            }
            settings["selectedTaskId"] = None
            settings["autoStartBreaks"] = False
            self._set_meta("autoStartLegacyDefaultUnknown", False)
            self._set_meta("settings", settings)
            self._set_meta(
                "snapshot",
                {"revision": 0, "canonicalTimer": None, "history": [], "tasks": [],
                 "knownTasks": [], "autoStartBreaks": False,
                 "selectedTaskId": None, "user": None},
            )

    def _reset_iroh_account_data(self, room_id: str) -> None:
        self._capture_local_iroh_records_locked(room_id)
        room = self.connection.execute(
            "SELECT return_workspace FROM iroh_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if room is None:
            raise ValueError("Active Iroh room workspace is missing.")
        returned = self._workspace_storage.deserialize(room["return_workspace"])
        return_snapshot = returned.get("metadata", {}).get("snapshot", {})
        preserve = isinstance(return_snapshot, dict) and return_snapshot.get("user") is None
        current = self._workspace_without_account(self._capture_workspace(), preserve_domain=True)
        returned = self._workspace_without_account(returned, preserve_domain=preserve)
        self.connection.execute(
            "UPDATE iroh_rooms SET return_workspace = ?, workspace = ? WHERE room_id = ?",
            (
                self._workspace_storage.serialize(returned),
                self._workspace_storage.serialize(current),
                room_id,
            ),
        )
        self._restore_workspace(current)

    def _clear_account_queues(self) -> None:
        for table in ("pending_commands", "pending_task_operations",
                      "pending_duration_operations", "pending_auto_start_operations",
                      "pending_selected_task_operations", "pending_auto_breaks",
                      "pending_auto_break_starts", "pending_phase_advances"):
            self.connection.execute(f"DELETE FROM {table}")
