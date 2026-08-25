from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


class IrohRecordPersistence:
    def __init__(
        self,
        connection: sqlite3.Connection,
        physical_time_ms: Callable[[], int],
    ) -> None:
        self._connection = connection
        self._physical_time_ms = physical_time_ms

    @staticmethod
    def validate_advertised(
        records: list[dict[str, Any]],
        advertised: dict[tuple[str, str], str],
    ) -> None:
        from .iroh_protocol import record_digest, record_id

        returned = {
            (record["domain"], record_id(record)): record_digest(record)
            for record in records
        }
        if returned != advertised:
            raise ValueError(
                "Fetched Iroh records do not match advertised inventory digests."
            )

    def insert_locked(self, room_id: str, records: list[dict[str, Any]]) -> bool:
        from .iroh_protocol import ImmutableConflict

        self._validate_insert_room(room_id, len(records), ImmutableConflict)
        prepared = self._prepare(records)
        self._check_record_conflicts(room_id, prepared, ImmutableConflict)
        self._check_timer_sequences(room_id, prepared)
        return self._insert_prepared(room_id, prepared)

    def _validate_insert_room(
        self,
        room_id: str,
        record_count: int,
        conflict_type: type[Exception],
    ) -> None:
        from .iroh_protocol import MAX_OPERATION_REFS

        if record_count > MAX_OPERATION_REFS:
            raise ValueError("Iroh operation batch exceeds 256 records.")
        room = self._connection.execute(
            "SELECT conflict FROM iroh_rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
        if room is None:
            raise ValueError("Iroh room does not exist.")
        if room["conflict"] is not None:
            raise conflict_type(
                "Iroh room requires repair before replication can continue."
            )

    @staticmethod
    def _prepare(
        records: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str, str]]:
        from .iroh_protocol import record_digest, record_id, validate_record

        prepared = []
        keys = set()
        for record in records:
            validate_record(record)
            identifier = record_id(record)
            key = (record["domain"], identifier)
            if key in keys:
                raise ValueError(
                    "Iroh operation batch contains duplicate references."
                )
            keys.add(key)
            prepared.append((record, identifier, record_digest(record)))
        return prepared

    def _check_record_conflicts(
        self,
        room_id: str,
        prepared: list[tuple[dict[str, Any], str, str]],
        conflict_type: type[Exception],
    ) -> None:
        for record, identifier, digest in prepared:
            existing = self._connection.execute(
                "SELECT digest, record FROM iroh_records WHERE room_id = ? "
                "AND domain = ? AND operation_id = ?",
                (room_id, record["domain"], identifier),
            ).fetchone()
            if existing is not None and existing["digest"] != digest:
                self._persist_record_conflict(
                    room_id,
                    record,
                    identifier,
                    digest,
                    str(existing["digest"]),
                )
                raise conflict_type(
                    "Iroh room contains different immutable payloads for the same "
                    "operation ID."
                )

    def _persist_record_conflict(
        self,
        room_id: str,
        record: dict[str, Any],
        identifier: str,
        received_digest: str,
        local_digest: str,
    ) -> None:
        evidence = {
            "domain": record["domain"],
            "id": identifier,
            "localDigest": local_digest,
            "receivedDigest": received_digest,
            "detectedAtMs": self._physical_time_ms(),
        }
        self._connection.execute(
            "INSERT OR IGNORE INTO iroh_conflicts(room_id, domain, operation_id, "
            "local_digest, received_digest, received_record, detected_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                record["domain"],
                identifier,
                local_digest,
                received_digest,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                evidence["detectedAtMs"],
            ),
        )
        self._connection.execute(
            "UPDATE iroh_rooms SET conflict = ? WHERE room_id = ?",
            (json.dumps(evidence, separators=(",", ":")), room_id),
        )

    def _check_timer_sequences(
        self,
        room_id: str,
        prepared: list[tuple[dict[str, Any], str, str]],
    ) -> None:
        sequence_owners: dict[tuple[str, int], str] = {}
        for row in self._connection.execute(
            "SELECT device_id, operation_id, record FROM iroh_records "
            "WHERE room_id = ? AND domain = 'timer'",
            (room_id,),
        ):
            record = json.loads(row["record"])
            sequence_owners[
                (str(row["device_id"]), int(record["operation"]["deviceSequence"]))
            ] = str(row["operation_id"])
        for record, identifier, _digest in prepared:
            if record["domain"] != "timer":
                continue
            key = (record["deviceId"], int(record["operation"]["deviceSequence"]))
            owner = sequence_owners.get(key)
            if owner is not None and owner != identifier:
                raise ValueError("Iroh timer operation reuses a device sequence.")
            sequence_owners[key] = identifier

    def _insert_prepared(
        self,
        room_id: str,
        prepared: list[tuple[dict[str, Any], str, str]],
    ) -> bool:
        inserted = False
        for record, identifier, digest in prepared:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO iroh_records(room_id, domain, operation_id, "
                "device_id, digest, record) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    record["domain"],
                    identifier,
                    record["deviceId"],
                    digest,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            inserted = inserted or cursor.rowcount > 0
        return inserted

    def inventory(
        self,
        room_id: str,
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, str]], str | None]:
        from .iroh_protocol import MAX_INVENTORY

        if isinstance(limit, bool) or not 1 <= limit <= MAX_INVENTORY:
            raise ValueError("Iroh inventory limit must be 1 through 1024.")
        parameters: list[Any] = [room_id]
        where = "room_id = ?"
        if after is not None:
            if not isinstance(after, str) or after.count("\0") != 1:
                raise ValueError("Iroh inventory cursor is invalid.")
            domain, identifier = after.split("\0")
            where += " AND (domain > ? OR (domain = ? AND operation_id > ?))"
            parameters.extend((domain, domain, identifier))
        rows = self._connection.execute(
            "SELECT domain, operation_id, digest FROM iroh_records WHERE "
            + where
            + " ORDER BY domain, operation_id LIMIT ?",
            (*parameters, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        entries = [
            {
                "domain": str(row["domain"]),
                "id": str(row["operation_id"]),
                "digest": str(row["digest"]),
            }
            for row in rows
        ]
        next_cursor = (
            f"{rows[-1]['domain']}\0{rows[-1]['operation_id']}"
            if has_more and rows
            else None
        )
        return entries, next_cursor

    def operations(
        self,
        room_id: str,
        references: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        from .iroh_protocol import MAX_OPERATION_REFS

        if not 1 <= len(references) <= MAX_OPERATION_REFS:
            raise ValueError(
                "Iroh operation request must contain 1 through 256 references."
            )
        keys = [(item.get("domain"), item.get("id")) for item in references]
        if len(keys) != len(set(keys)):
            raise ValueError("Iroh operation request contains duplicate references.")
        records = []
        for domain, identifier in keys:
            row = self._connection.execute(
                "SELECT record FROM iroh_records WHERE room_id = ? AND domain = ? "
                "AND operation_id = ?",
                (room_id, domain, identifier),
            ).fetchone()
            if row is None:
                raise KeyError("Requested Iroh operation was not found.")
            records.append(json.loads(row["record"]))
        return records

    def missing_references_locked(
        self,
        room_id: str,
        remote_entries: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], Exception | None]:
        from .iroh_protocol import MAX_INVENTORY, ImmutableConflict

        if len(remote_entries) > MAX_INVENTORY:
            raise ValueError("Iroh inventory exceeds 1024 entries.")
        missing = []
        for entry in remote_entries:
            row = self._connection.execute(
                "SELECT digest FROM iroh_records WHERE room_id = ? AND domain = ? "
                "AND operation_id = ?",
                (room_id, entry["domain"], entry["id"]),
            ).fetchone()
            if row is None:
                missing.append({"domain": entry["domain"], "id": entry["id"]})
                continue
            if row["digest"] == entry["digest"]:
                continue
            self._record_inventory_conflict(room_id, entry, str(row["digest"]))
            return missing, ImmutableConflict(
                "Iroh room inventory contains an immutable-ID conflict."
            )
        return missing, None

    def _record_inventory_conflict(
        self,
        room_id: str,
        entry: dict[str, str],
        local_digest: str,
    ) -> None:
        evidence = {
            "domain": entry["domain"],
            "id": entry["id"],
            "localDigest": local_digest,
            "receivedDigest": entry["digest"],
            "detectedAtMs": self._physical_time_ms(),
        }
        self._connection.execute(
            "INSERT OR IGNORE INTO iroh_conflicts(room_id, domain, operation_id, "
            "local_digest, received_digest, received_record, detected_at_ms) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (
                room_id,
                entry["domain"],
                entry["id"],
                local_digest,
                entry["digest"],
                evidence["detectedAtMs"],
            ),
        )
        self._connection.execute(
            "UPDATE iroh_rooms SET conflict = ? WHERE room_id = ?",
            (json.dumps(evidence, separators=(",", ":")), room_id),
        )
