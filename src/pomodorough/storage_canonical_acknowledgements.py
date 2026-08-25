from __future__ import annotations

import sqlite3
from typing import Any, Protocol

from .storage_model import ACKNOWLEDGEMENT_OUTCOMES

_ACKNOWLEDGEMENT_FIELDS = {
    "commands": ("acknowledgements", "commandId"),
    "taskOperations": ("taskAcknowledgements", "operationId"),
    "durationOperations": ("durationAcknowledgements", "operationId"),
    "autoStartOperations": ("autoStartAcknowledgements", "operationId"),
    "selectedTaskOperations": ("selectedTaskAcknowledgements", "operationId"),
}


class CanonicalAcknowledgementDependencies(Protocol):
    connection: sqlite3.Connection

    def _normalize_settings(self, value: Any) -> dict[str, Any]: ...

    def _set_meta(self, key: str, value: Any) -> None: ...

    def get_meta(self, key: str, default: Any = None) -> Any: ...


class CanonicalAcknowledgementHooks(Protocol):
    def _validate_acknowledgements(
        self,
        request_items: Any,
        response_items: Any,
        acknowledgement_id_key: str,
        label: str,
    ) -> list[dict[str, Any]]: ...


def validate_acknowledgements(
    request_items: Any,
    response_items: Any,
    acknowledgement_id_key: str,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(request_items, list) or not isinstance(response_items, list):
        raise ValueError(  # noqa: TRY004
            f"Sync returned invalid {label} acknowledgements."
        )
    sent_ids = [
        item.get("id") if isinstance(item, dict) else None for item in request_items
    ]
    acknowledged_ids: list[str] = []
    normalized_response_items: list[dict[str, Any]] = []
    for acknowledgement in response_items:
        if not isinstance(acknowledgement, dict):
            raise ValueError(  # noqa: TRY004
                f"Sync returned invalid {label} acknowledgements."
            )
        normalized_acknowledgement = {
            **acknowledgement,
            "reason": acknowledgement.get("reason", ""),
        }
        if (
            not isinstance(normalized_acknowledgement.get(acknowledgement_id_key), str)
            or normalized_acknowledgement.get("outcome") not in ACKNOWLEDGEMENT_OUTCOMES
            or not isinstance(normalized_acknowledgement["reason"], str)
        ):
            raise ValueError(f"Sync returned invalid {label} acknowledgements.")
        acknowledged_ids.append(normalized_acknowledgement[acknowledgement_id_key])
        normalized_response_items.append(normalized_acknowledgement)
    if (
        any(not isinstance(item_id, str) for item_id in sent_ids)
        or len(sent_ids) != len(set(sent_ids))
        or len(acknowledged_ids) != len(set(acknowledged_ids))
        or set(acknowledged_ids) != set(sent_ids)
    ):
        raise ValueError(f"Sync returned an invalid {label} acknowledgement set.")
    return normalized_response_items


def retained_queue_ids(
    domain: str,
    local: dict[str, dict[str, dict[str, Any]]],
    canonical: dict[str, Any],
    dropped: set[str],
) -> set[str]:
    acknowledgement_key, acknowledgement_id_key = _ACKNOWLEDGEMENT_FIELDS[domain]
    retained = set(local[domain]) - {
        item[acknowledgement_id_key] for item in canonical[acknowledgement_key]
    }
    retained -= dropped if domain == "commands" else set()
    return retained


def validate_reconciliation_queues(
    normalized: dict[str, list[dict[str, Any]]],
    local: dict[str, dict[str, dict[str, Any]]],
    canonical: dict[str, Any],
    dropped: set[str],
    invalid: ValueError,
) -> None:
    for domain, operations in normalized.items():
        ids = [str(item.get("id", "")) for item in operations]
        expected = retained_queue_ids(domain, local, canonical, dropped)
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise invalid
        allowed = {"occurredAt", "hlcWallMs", "hlcCounter"}
        if domain == "commands":
            allowed |= {"phase", "plannedDurationMs", "observedElapsedMs"}
        for operation in operations:
            original = local[domain][str(operation["id"])]
            if any(
                original.get(key) != operation.get(key)
                for key in (set(original) | set(operation)) - allowed
            ):
                raise invalid


class CanonicalAcknowledgementStorage:
    def __init__(
        self,
        store: CanonicalAcknowledgementDependencies,
        hooks: CanonicalAcknowledgementHooks,
    ) -> None:
        self._store = store
        self._hooks = hooks

    def _validated_sync_acknowledgements(
        self,
        response: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        specs = (
            ("acknowledgements", "commands", "commandId", "command", None),
            (
                "taskAcknowledgements",
                "taskOperations",
                "operationId",
                "task",
                None,
            ),
            (
                "durationAcknowledgements",
                "durationOperations",
                "operationId",
                "duration",
                None,
            ),
            (
                "autoStartAcknowledgements",
                "autoStartOperations",
                "operationId",
                "auto-start",
                [],
            ),
            (
                "selectedTaskAcknowledgements",
                "selectedTaskOperations",
                "operationId",
                "selected-task",
                [],
            ),
        )
        return {
            response_key: self._hooks._validate_acknowledgements(
                request.get(request_key, default)
                if default is not None
                else request.get(request_key),
                response[response_key],
                id_key,
                label,
            )
            for response_key, request_key, id_key, label, default in specs
        }

    def _reconcile_selected_phase_advances(
        self,
        canonical: dict[str, Any],
        discarded_command_ids: set[str] | None = None,
    ) -> None:
        acknowledgements = {
            acknowledgement["commandId"]: acknowledgement
            for acknowledgement in canonical["acknowledgements"]
        }
        discarded_command_ids = discarded_command_ids or set()
        canonical_timer = canonical["canonicalTimer"]
        rows = self._store.connection.execute(
            "SELECT finish_command_id, timer_id, source_phase, advanced_phase, "
            "selected_phase_version FROM pending_phase_advances"
        ).fetchall()
        for row in rows:
            finish_id = str(row["finish_command_id"])
            acknowledgement = acknowledgements.get(finish_id)
            discarded = finish_id in discarded_command_ids
            if acknowledgement is None and not discarded:
                continue
            exact_completion = self._has_exact_completion(
                canonical, canonical_timer, row, finish_id
            )
            non_applied = discarded or (
                acknowledgement is not None and acknowledgement["outcome"] != "applied"
            )
            if non_applied and not exact_completion:
                self._restore_selected_phase(row)
            self._store.connection.execute(
                "DELETE FROM pending_phase_advances WHERE finish_command_id = ?",
                (finish_id,),
            )

    @staticmethod
    def _has_exact_completion(
        canonical: dict[str, Any],
        canonical_timer: dict[str, Any] | None,
        row: sqlite3.Row,
        finish_id: str,
    ) -> bool:
        return any(
            item.get("timerId") == row["timer_id"]
            and item.get("commandId") == finish_id
            and item.get("phase") == row["source_phase"]
            and item.get("status") == "completed"
            for item in canonical["history"]
        ) or (
            canonical_timer is not None
            and canonical_timer.get("id") == row["timer_id"]
            and canonical_timer.get("phase") == row["source_phase"]
            and canonical_timer.get("status") == "completed"
            and isinstance(canonical_timer.get("lastIntent"), dict)
            and canonical_timer["lastIntent"].get("commandId") == finish_id
        )

    def _restore_selected_phase(self, row: sqlite3.Row) -> None:
        settings = self._store._normalize_settings(self._store.get_meta("settings", {}))
        if settings["selectedPhase"] == row["advanced_phase"] and int(
            self._store.get_meta("selectedPhaseVersion", 0)
        ) == int(row["selected_phase_version"]):
            settings["selectedPhase"] = row["source_phase"]
            self._store._set_meta("settings", settings)

    def _reconcile_unmaterialized_auto_break_triggers(
        self,
        canonical: dict[str, Any],
        discarded_command_ids: set[str] | None = None,
    ) -> None:
        acknowledgements = {
            acknowledgement["commandId"]: acknowledgement
            for acknowledgement in canonical["acknowledgements"]
        }
        discarded_command_ids = discarded_command_ids or set()
        canonical_timer = canonical["canonicalTimer"]
        rows = self._unmaterialized_auto_break_rows()
        for row in rows:
            finish_id = str(row["finish_command_id"])
            acknowledgement = acknowledgements.get(finish_id)
            discarded = finish_id in discarded_command_ids
            if acknowledgement is None and not discarded:
                continue
            accepted = self._auto_break_trigger_accepted(
                canonical,
                canonical_timer,
                row,
                finish_id,
                acknowledgement,
                discarded,
            )
            if not accepted:
                self._store.connection.execute(
                    "DELETE FROM pending_auto_breaks WHERE finish_command_id = ?",
                    (finish_id,),
                )

    def _unmaterialized_auto_break_rows(self) -> list[sqlite3.Row]:
        return self._store.connection.execute(
            "SELECT triggers.finish_command_id, triggers.timer_id "
            "FROM pending_auto_breaks AS triggers "
            "LEFT JOIN pending_auto_break_starts AS starts "
            "ON starts.source_finish_command_id = triggers.finish_command_id "
            "WHERE starts.source_finish_command_id IS NULL"
        ).fetchall()

    @staticmethod
    def _auto_break_trigger_accepted(
        canonical: dict[str, Any],
        canonical_timer: dict[str, Any] | None,
        row: sqlite3.Row,
        finish_id: str,
        acknowledgement: dict[str, Any] | None,
        discarded: bool,
    ) -> bool:
        timer_id = str(row["timer_id"])
        exact_completion = any(
            item.get("phase") == "focus"
            and item.get("status") == "completed"
            and item.get("timerId") == timer_id
            and item.get("commandId") == finish_id
            for item in canonical["history"]
        )
        timer_is_source = canonical_timer is not None and (
            canonical_timer.get("id") == timer_id
            and canonical_timer.get("phase") == "focus"
            and canonical_timer.get("status") == "completed"
        )
        return (
            not discarded
            and acknowledgement is not None
            and acknowledgement["outcome"] in {"applied", "ignored"}
            and exact_completion
            and timer_is_source
        )

    def _apply_acknowledgements(
        self,
        canonical: dict[str, Any],
        *,
        delete: bool = True,
    ) -> list[str]:
        notices: list[str] = []
        groups = (
            (
                "acknowledgements",
                "DELETE FROM pending_commands WHERE id = ?",
                "commandId",
            ),
            (
                "taskAcknowledgements",
                "DELETE FROM pending_task_operations WHERE id = ?",
                "operationId",
            ),
            (
                "durationAcknowledgements",
                "DELETE FROM pending_duration_operations WHERE id = ?",
                "operationId",
            ),
            (
                "autoStartAcknowledgements",
                "DELETE FROM pending_auto_start_operations WHERE id = ?",
                "operationId",
            ),
            (
                "selectedTaskAcknowledgements",
                "DELETE FROM pending_selected_task_operations WHERE id = ?",
                "operationId",
            ),
        )
        for response_key, delete_statement, id_key in groups:
            for acknowledgement in canonical[response_key]:
                if delete:
                    self._store.connection.execute(
                        delete_statement,
                        (acknowledgement[id_key],),
                    )
                if acknowledgement["outcome"] != "applied":
                    notices.append(
                        acknowledgement["reason"] or acknowledgement["outcome"]
                    )
        return notices
