from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from .secure_store import SecretMutationJournal
from .storage_replication_coordination import ReplicationTransactionCoordinator
from .storage_replication_projection import ReplicatedStateProjection
from .storage_workspace import WorkspacePersistence


@dataclass(frozen=True)
class RoomLifecycleDependencies:
    connection: sqlite3.Connection
    device_id: Callable[[], str]
    secret_store: Any
    read_meta: Callable[[str, Any], Any]
    write_meta: Callable[[str, Any], None]
    immediate_transaction: Callable[[], AbstractContextManager[None]]
    room_secret_key: Callable[[str], str]
    peer_ticket_key: Callable[[str, str], str]
    secure_reference: Callable[[str], bytes]
    workspace: WorkspacePersistence
    projection: ReplicatedStateProjection
    transactions: ReplicationTransactionCoordinator


class RoomWorkspaceLifecycle:
    def __init__(self, dependencies: RoomLifecycleDependencies) -> None:
        self._dependencies = dependencies

    @property
    def replication_mode(self) -> str:
        mode = self._dependencies.read_meta("replicationMode", "centralized")
        return mode if mode in {"offline", "iroh", "centralized"} else "centralized"

    @property
    def active_room_id(self) -> str | None:
        room_id = self._dependencies.read_meta("activeIrohRoomId", None)
        return room_id if isinstance(room_id, str) and room_id else None

    def create_room(
        self,
        room_secret: bytes,
        room_name: str | None = None,
        *,
        now_ms: int | None = None,
    ) -> str:
        from .iroh_protocol import room_id_for_secret

        if room_name is not None and not 1 <= len(room_name) <= 64:
            raise ValueError(
                "Room name must contain 1 through 64 Unicode scalar values."
            )
        room_id = room_id_for_secret(room_secret)
        with SecretMutationJournal(self._dependencies.secret_store) as secret_mutations:
            with self._dependencies.immediate_transaction():
                genesis = self._dependencies.projection.projected_local_genesis()
                record, digest = self._validated_genesis(genesis)
                return_workspace = self._dependencies.workspace.capture()
                room_workspace = self._dependencies.projection.empty_workspace(genesis)
                created_at = now_ms if now_ms is not None else int(time.time() * 1000)
                self._create_room_locked(
                    room_id,
                    room_secret,
                    room_name,
                    return_workspace,
                    room_workspace,
                    created_at,
                    record,
                    digest,
                    secret_mutations,
                )
        return room_id

    def _create_room_locked(
        self,
        room_id: str,
        room_secret: bytes,
        room_name: str | None,
        return_workspace: dict[str, Any],
        room_workspace: dict[str, Any],
        created_at: int,
        record: dict[str, Any],
        digest: str,
        secret_mutations: SecretMutationJournal,
    ) -> None:
        connection = self._dependencies.connection
        if connection.execute(
            "SELECT 1 FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone():
            raise ValueError("An Iroh room with this identity already exists.")
        room_secret_key = self._dependencies.room_secret_key(room_id)
        connection.execute(
            "INSERT INTO iroh_rooms(room_id, room_secret, room_name, "
            "return_workspace, workspace, created_at_ms, conflict) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                room_id,
                self._dependencies.secure_reference(room_secret_key),
                room_name,
                self._dependencies.workspace.serialize(return_workspace),
                self._dependencies.workspace.serialize(room_workspace),
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO iroh_records(room_id, domain, operation_id, device_id, "
            "digest, record) VALUES (?, 'genesis', 'genesis', ?, ?, ?)",
            (
                room_id,
                self._dependencies.device_id(),
                digest,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        self._dependencies.workspace.restore(room_workspace)
        self._dependencies.write_meta("activeIrohRoomId", room_id)
        self._dependencies.write_meta("replicationMode", "iroh")
        secret_mutations.save(room_secret_key, room_secret)

    def _validated_genesis(
        self,
        genesis: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        from .iroh_protocol import IrohProtocolError, record_digest, validate_record

        record = {
            "domain": "genesis",
            "deviceId": self._dependencies.device_id(),
            "operation": genesis,
        }
        try:
            validate_record(record)
            return record, record_digest(record)
        except IrohProtocolError as error:
            raise ValueError(str(error)) from error

    def prepare_join(
        self,
        room_id: str,
        room_secret: bytes,
        room_name: str | None,
        endpoint_id: str,
        endpoint_ticket: str,
        *,
        now_ms: int | None = None,
    ) -> None:
        from .iroh_protocol import room_id_for_secret, valid_room_id

        if (
            not valid_room_id(room_id)
            or room_id_for_secret(room_secret) != room_id
            or room_name is not None
            and not 1 <= len(room_name) <= 64
        ):
            raise ValueError("Iroh room metadata is invalid.")
        created_at = now_ms if now_ms is not None else int(time.time() * 1000)
        return_workspace = self._dependencies.workspace.capture()
        room_workspace = self._dependencies.projection.empty_joined_workspace(
            return_workspace
        )
        with SecretMutationJournal(self._dependencies.secret_store) as secret_mutations:
            with self._dependencies.immediate_transaction():
                self._prepare_join_locked(
                    room_id,
                    room_secret,
                    room_name,
                    endpoint_id,
                    endpoint_ticket,
                    return_workspace,
                    room_workspace,
                    created_at,
                    secret_mutations,
                )

    def _validate_existing_join_locked(
        self,
        room_id: str,
        room_secret: bytes,
    ) -> bool:
        existing = self._dependencies.connection.execute(
            "SELECT conflict FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if existing is None:
            return False
        if self.room_secret(room_id) != room_secret:
            raise ValueError("Saved Iroh room credentials do not match invite.")
        if existing["conflict"] is not None:
            raise ValueError("Saved Iroh room requires immutable-conflict repair.")
        return True

    def _prepare_join_locked(
        self,
        room_id: str,
        room_secret: bytes,
        room_name: str | None,
        endpoint_id: str,
        endpoint_ticket: str,
        return_workspace: dict[str, Any],
        room_workspace: dict[str, Any],
        created_at: int,
        secret_mutations: SecretMutationJournal,
    ) -> None:
        if self._validate_existing_join_locked(room_id, room_secret):
            self._dependencies.transactions.upsert_peer_locked(
                room_id,
                endpoint_id,
                endpoint_ticket,
                None,
                None,
                None,
                secret_mutations,
            )
            return
        connection = self._dependencies.connection
        room_secret_key = self._dependencies.room_secret_key(room_id)
        connection.execute(
            "INSERT INTO iroh_rooms(room_id, room_secret, room_name, "
            "return_workspace, workspace, created_at_ms, conflict) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                room_id,
                self._dependencies.secure_reference(room_secret_key),
                room_name,
                self._dependencies.workspace.serialize(return_workspace),
                self._dependencies.workspace.serialize(room_workspace),
                created_at,
            ),
        )
        self._dependencies.transactions.upsert_peer_locked(
            room_id,
            endpoint_id,
            endpoint_ticket,
            None,
            None,
            None,
            secret_mutations,
        )
        secret_mutations.save(room_secret_key, room_secret)

    def discard_inactive_room(self, room_id: str) -> None:
        with SecretMutationJournal(self._dependencies.secret_store) as secret_mutations:
            with self._dependencies.immediate_transaction():
                if self.active_room_id == room_id:
                    raise ValueError("Active Iroh room cannot be discarded.")
                conflict = self._dependencies.connection.execute(
                    "SELECT conflict FROM iroh_rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
                if conflict is not None and conflict["conflict"] is not None:
                    return
                peers = self._dependencies.connection.execute(
                    "SELECT endpoint_id FROM iroh_peers WHERE room_id = ?",
                    (room_id,),
                ).fetchall()
                self._dependencies.connection.execute(
                    "DELETE FROM iroh_rooms WHERE room_id = ?",
                    (room_id,),
                )
                secret_mutations.delete(self._dependencies.room_secret_key(room_id))
                for peer in peers:
                    secret_mutations.delete(
                        self._dependencies.peer_ticket_key(
                            room_id,
                            str(peer["endpoint_id"]),
                        )
                    )

    def activate_joined_room(self, room_id: str) -> None:
        with self._dependencies.immediate_transaction():
            room = self._dependencies.connection.execute(
                "SELECT workspace, conflict FROM iroh_rooms WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            genesis = self._dependencies.connection.execute(
                "SELECT 1 FROM iroh_records WHERE room_id = ? AND domain = 'genesis' "
                "AND operation_id = 'genesis'",
                (room_id,),
            ).fetchone()
            if room is None or genesis is None or room["conflict"] is not None:
                raise ValueError(
                    "Joined Iroh room has no valid genesis or requires repair."
                )
            projection = self._dependencies.projection.project_room(room_id)
            return_workspace = self._dependencies.workspace.capture()
            workspace = self._dependencies.projection.workspace_with_projection(
                self._dependencies.workspace.deserialize(room["workspace"]),
                projection,
            )
            self._dependencies.connection.execute(
                "UPDATE iroh_rooms SET return_workspace = ?, workspace = ? "
                "WHERE room_id = ?",
                (
                    self._dependencies.workspace.serialize(return_workspace),
                    self._dependencies.workspace.serialize(workspace),
                    room_id,
                ),
            )
            self._dependencies.workspace.restore(workspace)
            self._dependencies.write_meta("activeIrohRoomId", room_id)
            self._dependencies.write_meta("replicationMode", "iroh")

    def set_mode(self, mode: str) -> None:
        if mode not in {"offline", "iroh", "centralized"}:
            raise ValueError("Replication mode must be offline, iroh, or centralized.")
        with self._dependencies.immediate_transaction():
            current = self.replication_mode
            if current == mode:
                return
            if current == "iroh":
                self._deactivate_mode_locked()
            if mode == "iroh":
                self._activate_latest_mode_locked()
            self._dependencies.write_meta("replicationMode", mode)

    def _deactivate_mode_locked(self) -> None:
        room_id = self.active_room_id
        if room_id is None:
            raise ValueError("Active Iroh room metadata is missing.")
        room = self._dependencies.connection.execute(
            "SELECT return_workspace, conflict FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if room is None:
            raise ValueError("Active Iroh room workspace is missing.")
        if room["conflict"] is None:
            self._dependencies.transactions.capture_local_records_locked(room_id)
        else:
            workspace = self._dependencies.workspace.serialize(
                self._dependencies.workspace.capture()
            )
            self._dependencies.connection.execute(
                "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                (workspace, room_id),
            )
        self._dependencies.workspace.restore(
            self._dependencies.workspace.deserialize(room["return_workspace"])
        )
        self._dependencies.write_meta("activeIrohRoomId", None)

    def _activate_latest_mode_locked(self) -> None:
        room = self._dependencies.connection.execute(
            "SELECT room_id, workspace, conflict FROM iroh_rooms WHERE EXISTS "
            "(SELECT 1 FROM iroh_records AS records WHERE records.room_id = "
            "iroh_rooms.room_id AND records.domain = 'genesis' AND "
            "records.operation_id = 'genesis') ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        if room is None:
            raise ValueError("Create or join an Iroh room before selecting Iroh mode.")
        if room["conflict"] is not None:
            raise ValueError("Saved Iroh room requires repair before activation.")
        workspace = self._dependencies.workspace.serialize(
            self._dependencies.workspace.capture()
        )
        self._dependencies.connection.execute(
            "UPDATE iroh_rooms SET return_workspace = ? WHERE room_id = ?",
            (workspace, room["room_id"]),
        )
        self._dependencies.workspace.restore(
            self._dependencies.workspace.deserialize(room["workspace"])
        )
        self._dependencies.write_meta("activeIrohRoomId", str(room["room_id"]))

    def leave_room(self) -> None:
        if self.replication_mode != "iroh":
            raise ValueError("No Iroh room is active.")
        self.set_mode("offline")

    def room(self, room_id: str | None = None) -> dict[str, Any] | None:
        room_id = room_id or self.active_room_id
        if room_id is None:
            return None
        row = self._dependencies.connection.execute(
            "SELECT room_id, room_name, created_at_ms, conflict FROM iroh_rooms "
            "WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        peer_count = int(
            self._dependencies.connection.execute(
                "SELECT COUNT(*) AS count FROM iroh_peers WHERE room_id = ?",
                (room_id,),
            ).fetchone()["count"]
        )
        operation_count = int(
            self._dependencies.connection.execute(
                "SELECT COUNT(*) AS count FROM iroh_records WHERE room_id = ?",
                (room_id,),
            ).fetchone()["count"]
        )
        return {
            "roomId": str(row["room_id"]),
            "roomName": row["room_name"],
            "createdAtMs": int(row["created_at_ms"]),
            "peerCount": peer_count,
            "operationCount": operation_count,
            "conflict": json.loads(row["conflict"]) if row["conflict"] else None,
        }

    def room_secret(self, room_id: str) -> bytes:
        row = self._dependencies.connection.execute(
            "SELECT room_secret FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Saved Iroh room secret is unavailable or invalid.")
        secret = self._dependencies.secret_store.load(
            self._dependencies.room_secret_key(room_id)
        )
        if secret is None or len(secret) != 32:
            raise ValueError("Saved Iroh room secret is unavailable or invalid.")
        return secret
