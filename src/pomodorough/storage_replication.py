from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any, Protocol

from .storage_iroh_records import IrohRecordPersistence
from .storage_replication_coordination import (
    ReplicationTransactionCoordinator,
    ReplicationTransactionDependencies,
)
from .storage_replication_lifecycle import (
    RoomLifecycleDependencies,
    RoomWorkspaceLifecycle,
)
from .storage_replication_projection import (
    GeneratedBreakPlanner,
    ProjectionDependencies,
    ReplicatedStateProjection,
)
from .storage_workspace import WorkspacePersistence


def _iroh_conflict_time_ms() -> int:
    return int(time.time() * 1000)


class ReplicationStorageDependencies(Protocol):
    connection: sqlite3.Connection
    _iroh_secret_store: Any
    _shared_core: Any

    @property
    def device_id(self) -> str: ...

    def _display_minutes(self, duration_ms: int) -> int: ...
    def _immediate_transaction(self) -> Any: ...
    def _logical_clock(self, value: Any, **kwargs: Any) -> tuple[int, int]: ...
    def _normalize_settings(self, value: Any) -> dict[str, Any]: ...
    def _peer_ticket_key(self, room_id: str, endpoint_id: str) -> str: ...
    def _preflight_pending_queues(self) -> dict[str, Any]: ...
    def _project_operation(self, *args: Any, **kwargs: Any) -> Any: ...
    def _queue_command(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def _reserve_generation(
        self,
        physical_now_ms: int,
        *,
        sequence_count: int = 0,
        clock_count: int = 1,
        use_server_clock: bool = True,
        use_monotonic: bool = False,
    ) -> tuple[int, list[int], list[tuple[int, int]]]: ...
    def _reserve_uuid7_ids(self, wall_ms: int, count: int) -> list[str]: ...
    def _room_secret_key(self, room_id: str) -> str: ...
    def _secure_reference(self, key: str) -> bytes: ...
    def _set_meta(self, key: str, value: Any) -> None: ...
    def _valid_canonical_timer(self, timer: Any) -> bool: ...
    def _valid_history_item(self, item: Any) -> bool: ...
    def get_meta(self, key: str, default: Any = None) -> Any: ...
    def load(self, *, projection: bool = False) -> dict[str, Any]: ...


def _assemble_projection(
    dependencies: ReplicationStorageDependencies,
    workspace_storage: WorkspacePersistence,
    shared_core: Callable[[], Any],
) -> ReplicatedStateProjection:
    return ReplicatedStateProjection(
        ProjectionDependencies(
            connection=dependencies.connection,
            load_state=dependencies.load,
            read_meta=dependencies.get_meta,
            normalize_settings=dependencies._normalize_settings,
            logical_clock=dependencies._logical_clock,
            project_operation=dependencies._project_operation,
            display_minutes=dependencies._display_minutes,
            valid_canonical_timer=dependencies._valid_canonical_timer,
            valid_history_item=dependencies._valid_history_item,
            shared_core=shared_core,
            workspace=workspace_storage,
        )
    )


def _assemble_transactions(
    dependencies: ReplicationStorageDependencies,
    workspace_storage: WorkspacePersistence,
    record_storage: IrohRecordPersistence,
    device_id: Callable[[], str],
    projection: ReplicatedStateProjection,
    break_planner: GeneratedBreakPlanner,
) -> ReplicationTransactionCoordinator:
    return ReplicationTransactionCoordinator(
        ReplicationTransactionDependencies(
            connection=dependencies.connection,
            device_id=device_id,
            secret_store=dependencies._iroh_secret_store,
            read_meta=dependencies.get_meta,
            write_meta=dependencies._set_meta,
            immediate_transaction=dependencies._immediate_transaction,
            normalize_settings=dependencies._normalize_settings,
            preflight_pending_queues=dependencies._preflight_pending_queues,
            queue_command=dependencies._queue_command,
            reserve_generation=dependencies._reserve_generation,
            reserve_uuid7_ids=dependencies._reserve_uuid7_ids,
            peer_ticket_key=dependencies._peer_ticket_key,
            workspace=workspace_storage,
            records=record_storage,
            projection=projection,
            break_planner=break_planner,
        )
    )


def _assemble_rooms(
    dependencies: ReplicationStorageDependencies,
    workspace_storage: WorkspacePersistence,
    device_id: Callable[[], str],
    projection: ReplicatedStateProjection,
    transactions: ReplicationTransactionCoordinator,
) -> RoomWorkspaceLifecycle:
    return RoomWorkspaceLifecycle(
        RoomLifecycleDependencies(
            connection=dependencies.connection,
            device_id=device_id,
            secret_store=dependencies._iroh_secret_store,
            read_meta=dependencies.get_meta,
            write_meta=dependencies._set_meta,
            immediate_transaction=dependencies._immediate_transaction,
            room_secret_key=dependencies._room_secret_key,
            peer_ticket_key=dependencies._peer_ticket_key,
            secure_reference=dependencies._secure_reference,
            workspace=workspace_storage,
            projection=projection,
            transactions=transactions,
        )
    )


def _bind_component_interfaces(facade: ReplicationStorage) -> None:
    facade.create_iroh_room = facade._rooms.create_room
    facade.prepare_iroh_join = facade._rooms.prepare_join
    facade.discard_inactive_iroh_room = facade._rooms.discard_inactive_room
    facade.activate_joined_iroh_room = facade._rooms.activate_joined_room
    facade.set_replication_mode = facade._rooms.set_mode
    facade.leave_iroh_room = facade._rooms.leave_room
    facade.iroh_room = facade._rooms.room
    facade.iroh_room_secret = facade._rooms.room_secret
    facade.capture_local_iroh_records = facade._transactions.capture_local_records
    facade._capture_iroh_after_mutation = facade._transactions.capture_after_mutation
    facade._capture_iroh_after_mutation_locked = (
        facade._transactions.capture_after_mutation_locked
    )
    facade._capture_local_iroh_records_locked = (
        facade._transactions.capture_local_records_locked
    )
    facade.project_iroh_expiry = facade._transactions.project_expiry
    facade.insert_remote_iroh_records = facade._transactions.insert_remote_records
    facade.missing_iroh_references = facade._transactions.missing_references
    facade.upsert_iroh_peer = facade._transactions.upsert_peer
    facade.iroh_peers = facade._transactions.peers
    facade.has_pending_auto_break = facade._transactions.has_pending_auto_break
    facade._projected_local_genesis = facade._projection.projected_local_genesis


class ReplicationStorage:
    create_iroh_room: Callable[..., str]
    prepare_iroh_join: Callable[..., None]
    discard_inactive_iroh_room: Callable[[str], None]
    activate_joined_iroh_room: Callable[[str], None]
    set_replication_mode: Callable[[str], None]
    leave_iroh_room: Callable[[], None]
    iroh_room: Callable[..., dict[str, Any] | None]
    iroh_room_secret: Callable[[str], bytes]
    capture_local_iroh_records: Callable[[], bool]
    project_iroh_expiry: Callable[[int | None], bool]
    insert_remote_iroh_records: Callable[..., bool]
    missing_iroh_references: Callable[..., list[dict[str, str]]]
    upsert_iroh_peer: Callable[..., None]
    iroh_peers: Callable[[str], list[dict[str, Any]]]
    has_pending_auto_break: Callable[[], bool]

    def __init__(
        self,
        dependencies: ReplicationStorageDependencies,
        workspace_storage: WorkspacePersistence,
        record_storage: IrohRecordPersistence,
    ) -> None:
        self._read_meta = dependencies.get_meta

        def device_id() -> str:
            return str(dependencies.get_meta("deviceId"))

        def shared_core() -> Any:
            return dependencies._shared_core

        self._break_planner = GeneratedBreakPlanner()
        self._projection = _assemble_projection(
            dependencies,
            workspace_storage,
            shared_core,
        )
        self._transactions = _assemble_transactions(
            dependencies,
            workspace_storage,
            record_storage,
            device_id,
            self._projection,
            self._break_planner,
        )
        self._rooms = _assemble_rooms(
            dependencies,
            workspace_storage,
            device_id,
            self._projection,
            self._transactions,
        )
        _bind_component_interfaces(self)

    @property
    def replication_mode(self) -> str:
        mode = self._read_meta("replicationMode", "centralized")
        return mode if mode in {"offline", "iroh", "centralized"} else "centralized"

    @property
    def active_iroh_room_id(self) -> str | None:
        room_id = self._read_meta("activeIrohRoomId", None)
        return room_id if isinstance(room_id, str) and room_id else None
