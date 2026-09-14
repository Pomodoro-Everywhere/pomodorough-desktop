from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .secure_store import SecretMutationJournal
from .storage_iroh_records import IrohRecordPersistence
from .storage_replication_projection import (
    GeneratedBreakPlanner,
    ReplicatedStateProjection,
)
from .storage_workspace import WorkspacePersistence


@dataclass(frozen=True)
class ReplicationTransactionDependencies:
    connection: sqlite3.Connection
    device_id: Callable[[], str]
    secret_store: Any
    read_meta: Callable[[str, Any], Any]
    write_meta: Callable[[str, Any], None]
    retire_delivery_proof: Callable[[dict[str, list[str]]], None]
    immediate_transaction: Callable[[], AbstractContextManager[None]]
    normalize_settings: Callable[[Any], dict[str, Any]]
    preflight_pending_queues: Callable[[], dict[str, Any]]
    queue_command: Callable[..., dict[str, Any]]
    reserve_generation: Callable[..., tuple[int, list[int], list[tuple[int, int]]]]
    reserve_uuid7_ids: Callable[[int, int], list[str]]
    peer_ticket_key: Callable[[str, str], str]
    workspace: WorkspacePersistence
    records: IrohRecordPersistence
    projection: ReplicatedStateProjection
    break_planner: GeneratedBreakPlanner


def _validated_peer_capabilities(
    capabilities: list[str] | None,
) -> str | None:
    """Serialize hello capabilities; None means legacy/unknown peer."""
    if capabilities is None:
        return None
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 16
        or any(
            not isinstance(item, str) or not 1 <= len(item) <= 64
            for item in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
    ):
        raise ValueError("Iroh peer capabilities are invalid.")
    return json.dumps(capabilities, separators=(",", ":"))


def _parsed_peer_capabilities(value: Any) -> list[str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Saved Iroh peer capabilities are invalid.") from error
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) for item in parsed)
    ):
        raise ValueError("Saved Iroh peer capabilities are invalid.")
    return parsed


def _validate_peer_metadata(
    endpoint_id: str, endpoint_ticket: str, display_name: str | None
) -> None:
    from .iroh_protocol import MAX_ENDPOINT_TICKET
    if (
        not endpoint_id
        or not endpoint_ticket
        or len(endpoint_ticket.encode()) > MAX_ENDPOINT_TICKET
        or display_name is not None
        and not 1 <= len(display_name) <= 64
    ):
        raise ValueError("Iroh peer metadata is invalid.")


def _held_back_ids(
    records: list[dict[str, Any]], publishable: list[dict[str, Any]]
) -> dict[str, set[str]]:
    """Map held-back records to pending-table IDs for workspace retain."""
    published = {
        (record.get("domain"), record.get("operation", {}).get("id"))
        for record in publishable
    }
    table_for = {
        "timer": "pending_commands",
        "task": "pending_task_operations",
        "duration": "pending_duration_operations",
        "autoStart": "pending_auto_start_operations",
        "selectedTask": "pending_selected_task_operations",
    }
    held_ids: dict[str, set[str]] = {}
    for record in records:
        key = (record.get("domain"), record.get("operation", {}).get("id"))
        if key in published:
            continue
        table = table_for.get(str(record.get("domain", "")))
        operation_id = record.get("operation", {}).get("id")
        if table is None or not isinstance(operation_id, str):
            continue
        held_ids.setdefault(table, set()).add(operation_id)
    return held_ids


class ReplicationTransactionCoordinator:
    def __init__(self, dependencies: ReplicationTransactionDependencies) -> None:
        self._dependencies = dependencies

    def capture_local_records(self) -> bool:
        room_id = self._active_room_id()
        if self._replication_mode() != "iroh" or room_id is None:
            return False
        with self._dependencies.immediate_transaction():
            return self.capture_local_records_locked(room_id)

    def capture_after_mutation(self) -> None:
        with self._dependencies.immediate_transaction():
            self.capture_after_mutation_locked()

    def capture_after_mutation_locked(self) -> None:
        if self._replication_mode() == "iroh":
            room = self._room()
            if room is not None and room.get("conflict") is not None:
                self._dependencies.connection.execute(
                    "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                    (
                        self._dependencies.workspace.serialize(
                            self._dependencies.workspace.capture()
                        ),
                        room["roomId"],
                    ),
                )
                return
            self.capture_local_records_locked(room["roomId"])

    def _room(self) -> dict[str, Any] | None:
        room_id = self._active_room_id()
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

    def capture_local_records_locked(self, room_id: str) -> bool:
        records = self._pending_records()
        publishable = self._publishable_records(records, room_id)
        if not publishable:
            self._dependencies.workspace.save_room(
                room_id,
                self._dependencies.workspace.capture(),
            )
            return False
        self._dependencies.records.insert_locked(room_id, publishable)
        self._clear_captured_queues(publishable)
        capture = self._dependencies.workspace.capture()
        held_snapshot = deepcopy(capture)
        projection = self._dependencies.projection.project_room(room_id)
        workspace = self._dependencies.projection.workspace_with_projection(
            capture,
            projection,
        )
        self._retain_held_back(workspace, held_snapshot, records, publishable)
        self._dependencies.workspace.save_room(room_id, workspace)
        self._dependencies.workspace.restore(workspace)
        return True

    def _retain_held_back(
        self,
        workspace: dict[str, Any],
        capture: dict[str, Any],
        records: list[dict[str, Any]],
        publishable: list[dict[str, Any]],
    ) -> None:
        """Keep held-back retarget rows through the room workspace swap."""
        held_ids = _held_back_ids(records, publishable)
        if not held_ids:
            return
        capture_tables = capture.get("tables", {})
        workspace_tables = workspace.get("tables", {})
        for table, ids in held_ids.items():
            workspace_tables[table] = [
                row for row in capture_tables.get(table, [])
                if str(row.get("id")) in ids
            ]
        capture_physical = capture.get("metadata", {}).get(
            "commandPhysicalTimes", {}
        )
        if isinstance(capture_physical, dict):
            workspace["metadata"]["commandPhysicalTimes"] = {
                key: value for key, value in capture_physical.items()
                if key in held_ids.get("pending_commands", set())
            }
        self._retain_held_back_clock(workspace, capture)

    def _retain_held_back_clock(
        self, workspace: dict[str, Any], capture: dict[str, Any]
    ) -> None:
        """Keep the newer HLC so held-back clocks stay covered."""
        try:
            before = workspace["metadata"]["hlc"]
            after = capture["metadata"]["hlc"]
            before_key = (int(before["wallMs"]), int(before["counter"]))
            after_key = (int(after["wallMs"]), int(after["counter"]))
        except (KeyError, TypeError, ValueError):
            return
        if after_key > before_key:
            workspace["metadata"]["hlc"] = after

    def _publishable_records(
        self, records: list[dict[str, Any]], room_id: str
    ) -> list[dict[str, Any]]:
        """Hold back retargets until every known peer supports retarget-v1.

        Legacy/unknown peers would reject the new timer type, so the
        retarget stays queued locally for exact retry after the last
        peer upgrades. All other domains publish normally.
        """
        if self.peers_support(room_id, "retarget-v1"):
            return records
        return [
            record
            for record in records
            if not (
                record.get("domain") == "timer"
                and record.get("operation", {}).get("type") == "retarget"
            )
        ]

    def _pending_records(self) -> list[dict[str, Any]]:
        pending = self._dependencies.preflight_pending_queues()
        records = []
        for domain, operations in (
            ("timer", pending["commands"]),
            ("task", pending["taskOperations"]),
            ("duration", pending["durationOperations"]),
            ("autoStart", pending["autoStartOperations"]),
            ("selectedTask", pending["selectedTaskOperations"]),
        ):
            for operation in operations:
                wire_operation = deepcopy(operation)
                if domain in {"autoStart", "selectedTask"}:
                    wire_operation.pop("deviceId", None)
                records.append(
                    {
                        "domain": domain,
                        "deviceId": self._dependencies.device_id(),
                        "operation": wire_operation,
                    }
                )
        return records

    def _clear_captured_queues(self, records: list[dict[str, Any]]) -> None:
        retired: dict[str, list[str]] = {
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        }
        domains = {
            "timer": "commands",
            "task": "taskOperations",
            "duration": "durationOperations",
            "autoStart": "autoStartOperations",
            "selectedTask": "selectedTaskOperations",
        }
        for record in records:
            domain = domains.get(record.get("domain", ""))
            operation_id = record.get("operation", {}).get("id")
            if domain is not None and isinstance(operation_id, str):
                retired[domain].append(operation_id)
        self._dependencies.retire_delivery_proof(retired)
        self._delete_published_ids(retired)
        connection = self._dependencies.connection
        connection.executemany(
            "DELETE FROM pending_phase_advances WHERE finish_command_id = ?",
            ((identifier,) for identifier in retired["commands"]),
        )
        connection.execute("DELETE FROM pending_auto_break_starts")
        self._prune_published_physical_times(retired["commands"])
        self._dependencies.write_meta("pendingSync", None)

    def _delete_published_ids(self, retired: dict[str, list[str]]) -> None:
        """Delete only published queue rows; held-back retargets stay."""
        tables = {
            "commands": "pending_commands",
            "taskOperations": "pending_task_operations",
            "durationOperations": "pending_duration_operations",
            "autoStartOperations": "pending_auto_start_operations",
            "selectedTaskOperations": "pending_selected_task_operations",
        }
        connection = self._dependencies.connection
        for domain, table in tables.items():
            ids = retired.get(domain, [])
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM {table} WHERE id IN ({placeholders})",
                    tuple(ids),
                )

    def _prune_published_physical_times(self, command_ids: list[str]) -> None:
        if not command_ids:
            return
        physical = self._dependencies.read_meta("commandPhysicalTimes", {})
        if not isinstance(physical, dict):
            return
        published = set(command_ids)
        remaining = {
            key: value for key, value in physical.items() if key not in published
        }
        if remaining != physical:
            self._dependencies.write_meta("commandPhysicalTimes", remaining)

    def project_expiry(self, now_ms: int | None = None) -> bool:
        room_id = self._active_room_id()
        if self._replication_mode() != "iroh" or room_id is None:
            return False
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._dependencies.immediate_transaction():
            self.capture_local_records_locked(room_id)
            before = self._dependencies.read_meta("snapshot", {}).get("canonicalTimer")
            projection = self._dependencies.projection.project_room(
                room_id,
                now_ms=now_ms,
            )
            settings = self._dependencies.normalize_settings(
                self._dependencies.read_meta("settings", {})
            )
            plan = self._dependencies.break_planner.plan(
                before,
                projection,
                settings,
                self._dependencies.device_id(),
            )
            if plan.selected_phase is not None:
                settings["selectedPhase"] = plan.selected_phase
                self._dependencies.write_meta("settings", settings)
            workspace = self._dependencies.projection.workspace_with_projection(
                self._dependencies.workspace.capture(),
                projection,
            )
            self._dependencies.connection.execute(
                "UPDATE iroh_rooms SET workspace = ? WHERE room_id = ?",
                (self._dependencies.workspace.serialize(workspace), room_id),
            )
            self._dependencies.workspace.restore(workspace)
            if plan.generated_break_phase is not None:
                self._queue_generated_break(plan.generated_break_phase, now_ms)
                self.capture_local_records_locked(room_id)
            return plan.expired

    def _queue_generated_break(self, phase: str, now_ms: int) -> None:
        settings = self._dependencies.normalize_settings(
            self._dependencies.read_meta("settings", {})
        )
        trusted_ms, sequences, clocks = self._dependencies.reserve_generation(
            now_ms,
            sequence_count=1,
            clock_count=1,
            use_server_clock=False,
        )
        self._dependencies.queue_command(
            "start",
            None,
            phase,
            settings["durationsMs"],
            None,
            now_ms,
            timer_now_ms=now_ms,
            trusted_ms=trusted_ms,
            sequence=sequences[0],
            clock=clocks[0],
            command_id=self._dependencies.reserve_uuid7_ids(clocks[0][0], 1)[0],
        )

    def insert_remote_records(
        self,
        room_id: str,
        records: list[dict[str, Any]],
        advertised_digests: dict[tuple[str, str], str] | None = None,
    ) -> bool:
        if not records:
            raise ValueError("Iroh operation batch must not be empty.")
        conflict: Exception | None = None
        with self._dependencies.immediate_transaction():
            if advertised_digests is not None:
                self._dependencies.records.validate_advertised(
                    records,
                    advertised_digests,
                )
            active = (
                self._active_room_id() == room_id and self._replication_mode() == "iroh"
            )
            if active:
                self.capture_local_records_locked(room_id)
            try:
                inserted = self._dependencies.records.insert_locked(room_id, records)
            except Exception as error:
                if error.__class__.__name__ != "ImmutableConflict":
                    raise
                inserted = False
                conflict = error
            if inserted:
                workspace = self._refresh_workspace(room_id)
                if active:
                    self._dependencies.workspace.restore(workspace)
        if conflict is not None:
            raise conflict
        return inserted

    def _refresh_workspace(self, room_id: str) -> dict[str, Any]:
        projection = self._dependencies.projection.project_room(room_id)
        room = self._dependencies.connection.execute(
            "SELECT workspace FROM iroh_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if room is None:
            raise ValueError("Iroh room workspace is missing.")
        workspace = self._dependencies.projection.workspace_with_projection(
            self._dependencies.workspace.deserialize(room["workspace"]),
            projection,
        )
        self._dependencies.workspace.save_room(room_id, workspace)
        return workspace

    def missing_references(
        self,
        room_id: str,
        remote_entries: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        conflict: Exception | None = None
        with self._dependencies.immediate_transaction():
            missing, conflict = self._dependencies.records.missing_references_locked(
                room_id,
                remote_entries,
            )
        if conflict is not None:
            raise conflict
        return missing

    def upsert_peer_locked(
        self,
        room_id: str,
        endpoint_id: str,
        endpoint_ticket: str,
        device_id: str | None,
        display_name: str | None,
        last_seen_at_ms: int | None,
        secret_mutations: SecretMutationJournal,
        capabilities: list[str] | None = None,
    ) -> None:
        from .iroh_protocol import MAX_PEERS

        _validate_peer_metadata(endpoint_id, endpoint_ticket, display_name)
        stored_capabilities = _validated_peer_capabilities(capabilities)
        exists = self._dependencies.connection.execute(
            "SELECT 1 FROM iroh_peers WHERE room_id = ? AND endpoint_id = ?",
            (room_id, endpoint_id),
        ).fetchone()
        count = self._dependencies.connection.execute(
            "SELECT COUNT(*) AS count FROM iroh_peers WHERE room_id = ?",
            (room_id,),
        ).fetchone()["count"]
        if exists is None and count >= MAX_PEERS:
            raise ValueError("Iroh room address book contains 64 peers.")
        ticket_key = self._dependencies.peer_ticket_key(room_id, endpoint_id)
        secret_mutations.save(
            ticket_key,
            endpoint_ticket.encode("utf-8"),
        )
        self._dependencies.connection.execute(
            "INSERT INTO iroh_peers(room_id, endpoint_id, endpoint_ticket, "
            "device_id, display_name, capabilities, last_seen_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(room_id, endpoint_id) DO UPDATE SET "
            "endpoint_ticket = excluded.endpoint_ticket, "
            "device_id = excluded.device_id, "
            "display_name = excluded.display_name, "
            "capabilities = excluded.capabilities, "
            "last_seen_at_ms = excluded.last_seen_at_ms",
            (
                room_id,
                endpoint_id,
                f"secure:{ticket_key}",
                device_id,
                display_name,
                stored_capabilities,
                last_seen_at_ms,
            ),
        )

    def upsert_peer(
        self,
        room_id: str,
        endpoint_id: str,
        endpoint_ticket: str,
        device_id: str | None,
        display_name: str | None,
        last_seen_at_ms: int | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        with SecretMutationJournal(self._dependencies.secret_store) as mutations:
            with self._dependencies.immediate_transaction():
                self.upsert_peer_locked(
                    room_id,
                    endpoint_id,
                    endpoint_ticket,
                    device_id,
                    display_name,
                    last_seen_at_ms,
                    mutations,
                    capabilities,
                )

    def peers(self, room_id: str) -> list[dict[str, Any]]:
        peers = []
        for row in self._dependencies.connection.execute(
            "SELECT endpoint_id, endpoint_ticket, device_id, display_name, "
            "capabilities, last_seen_at_ms FROM iroh_peers WHERE room_id = ? "
            "ORDER BY last_seen_at_ms DESC, endpoint_id",
            (room_id,),
        ):
            endpoint_id = str(row["endpoint_id"])
            ticket = self._dependencies.secret_store.load(
                self._dependencies.peer_ticket_key(room_id, endpoint_id)
            )
            if ticket is None:
                raise ValueError("Saved Iroh peer capability is unavailable.")
            try:
                endpoint_ticket = ticket.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("Saved Iroh peer capability is invalid.") from error
            peers.append(
                {
                    "endpointId": endpoint_id,
                    "endpointTicket": endpoint_ticket,
                    "deviceId": row["device_id"],
                    "displayName": row["display_name"],
                    "capabilities": _parsed_peer_capabilities(
                        row["capabilities"] if "capabilities" in row.keys() else None
                    ),
                    "lastSeenAtMs": row["last_seen_at_ms"],
                }
            )
        return peers

    def peers_support(self, room_id: str, capability: str) -> bool:
        """True when every known peer explicitly advertises a capability.

        Missing (legacy/unknown) counts as unsupported: retarget records
        must not be published into a room with legacy peers. Rollout
        ordering is upgrade-all-devices-first; held-back retargets stay
        queued locally for exact retry after the last peer upgrades.
        """
        return all(
            isinstance(peer.get("capabilities"), list)
            and capability in peer["capabilities"]
            for peer in self.peers(room_id)
        )

    def has_pending_auto_break(self) -> bool:
        return (
            self._dependencies.connection.execute(
                "SELECT 1 FROM pending_auto_breaks LIMIT 1"
            ).fetchone()
            is not None
        )

    def _replication_mode(self) -> str:
        mode = self._dependencies.read_meta("replicationMode", "centralized")
        return mode if mode in {"offline", "iroh", "centralized"} else "centralized"

    def _active_room_id(self) -> str | None:
        room_id = self._dependencies.read_meta("activeIrohRoomId", None)
        return room_id if isinstance(room_id, str) and room_id else None
