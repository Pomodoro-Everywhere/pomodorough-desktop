"""Regression coverage for the immutable reconcile.rebase.v2 contract.

Covers in-flight acknowledgement, lost-response retry, restart recovery,
already-synced Start convergence, peer/Iroh capture, and history
convergence. The pinned production bundle (Core 0.38.0) serves v2, so the
production-bundle tests run against the real SharedCore; the client-glue
tests keep the spec-derived double (tests/v2_core_double.py) for delivery
mechanics, and one stub test pins fail-closed behavior for stale bundles
with no v1 fallback.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pomodorough.core import task_from_title
from pomodorough.iroh_protocol import record_digest, record_id
from pomodorough.shared_core import SharedCoreOperationError
from pomodorough.storage import Store, utc_timestamp
from v2_core_double import V2EmulatingSharedCore


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, key: str) -> bytes | None:
        return self.values.get(key)

    def save(self, key: str, value: bytes) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _canonical_response(
    store: Store, request: dict[str, object], **overrides: object
) -> dict[str, object]:
    specs = (
        ("acknowledgements", "commands", "commandId"),
        ("taskAcknowledgements", "taskOperations", "operationId"),
        ("durationAcknowledgements", "durationOperations", "operationId"),
        ("autoStartAcknowledgements", "autoStartOperations", "operationId"),
        ("selectedTaskAcknowledgements", "selectedTaskOperations", "operationId"),
    )
    response: dict[str, object] = {
        response_key: [
            {id_key: item["id"], "outcome": "applied", "reason": ""}
            for item in request[request_key]  # type: ignore[index]
        ]
        for response_key, request_key, id_key in specs
    }
    wall_ms = max(
        (
            item["hlcWallMs"]
            for key in (
                "commands",
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
                "selectedTaskOperations",
            )
            for item in request.get(key, [])
            if item.get("hlcWallMs", 0) > 0
        ),
        default=1_000,
    )
    response.update(
        revision=1,
        canonicalTimer=None,
        history=[],
        tasks=[],
        durationsMs={
            "focus": 25 * 60_000,
            "short_break": 5 * 60_000,
            "long_break": 15 * 60_000,
        },
        autoStartBreaks=False,
        selectedTaskId=None,
        serverTime=utc_timestamp(wall_ms),
        serverHlcWallMs=wall_ms,
        serverHlcCounter=0,
    )
    response.update(overrides)
    return response


class ImmutableReconcileV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.secrets = MemorySecretStore()
        self.store = Store(
            self.path,
            iroh_secret_store=self.secrets,
            shared_core=V2EmulatingSharedCore(),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _start_with_retarget(self, now_ms: int = 1_000):
        first = task_from_title("Immut first")
        second = task_from_title("Immut second")
        self.store.queue_task_operation("upsert", first, now_ms=1)
        self.store.queue_task_operation("upsert", second, now_ms=2)
        self.store.set_selected_task_id(first["id"], now_ms=3)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], first["id"],
            now_ms=now_ms,
        )
        self.store.set_selected_task_id(second["id"], now_ms=now_ms + 1)
        pending = self.store.load()["pending"]
        retarget = next(c for c in pending if c["type"] == "retarget")
        return first, second, start, retarget

    def test_in_flight_ack_consumes_start_and_preserves_retarget(self) -> None:
        first = task_from_title("Immut first")
        second = task_from_title("Immut second")
        self.store.queue_task_operation("upsert", first, now_ms=1)
        self.store.queue_task_operation("upsert", second, now_ms=2)
        self.store.set_selected_task_id(first["id"], now_ms=3)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], first["id"],
            now_ms=1_000,
        )
        start_payload = deepcopy(start)
        request = self.store.sync_payload()
        self.assertIn(start["id"], [c["id"] for c in request["commands"]])
        # Retarget queued in flight: never sent, keeps durable proof.
        self.store.set_selected_task_id(second["id"], now_ms=1_001)
        pending = self.store.load()["pending"]
        retarget = next(c for c in pending if c["type"] == "retarget")
        # The claim retired proof for the exact outgoing batch only.
        proof = self.store.delivery_proof()
        self.assertNotIn(start["id"], proof["commands"])
        self.assertIn(retarget["id"], proof["commands"])
        response = _canonical_response(
            self.store,
            request,
            revision=1,
            canonicalTimer={
                "id": start["timerId"],
                "phase": "focus",
                "status": "running",
                "plannedDurationMs": start["plannedDurationMs"],
                "elapsedAtAnchorMs": 0,
                "anchorAt": start["occurredAt"],
                "taskId": first["id"],
                "startedByDeviceId": self.store.device_id,
                "lastIntent": {
                    "type": "start",
                    "commandId": start["id"],
                    "occurredAt": start["occurredAt"],
                },
            },
        )
        # Before the claim, the never-sent retarget projects optimistically.
        pre_state = self.store.load(projection=True)
        pre_projection = self.store.projected_state(now_ms=1_002, state=pre_state)
        self.assertEqual(pre_projection.canonical_timer.get("taskId"), second["id"])
        self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [retarget])
        # The acknowledged Start left without touching the retarget payload.
        self.assertEqual(start, start_payload)
        self.assertEqual(self.store.canonical_head(), (1_000, 0))
        follow_up = self.store.sync_payload()
        self.assertEqual(follow_up["commands"], [retarget])
        # The claimed retarget is possibly delivered: it stays available
        # for exact retry but no longer overwrites the canonical snapshot.
        claimed_state = self.store.load(projection=True)
        claimed_projection = self.store.projected_state(
            now_ms=1_002, state=claimed_state
        )
        self.assertEqual(
            claimed_projection.canonical_timer.get("taskId"), first["id"]
        )
        self.store.apply_sync(
            _canonical_response(
                self.store,
                follow_up,
                revision=2,
                canonicalTimer={
                    "id": start["timerId"],
                    "phase": "focus",
                    "status": "running",
                    "plannedDurationMs": start["plannedDurationMs"],
                    "elapsedAtAnchorMs": 0,
                    "anchorAt": start["occurredAt"],
                    "taskId": second["id"],
                    "startedByDeviceId": self.store.device_id,
                    "lastIntent": {
                        "type": "start",
                        "commandId": start["id"],
                        "occurredAt": start["occurredAt"],
                    },
                },
                tasks=[
                    {"id": first["id"], "title": first["title"]},
                    {"id": second["id"], "title": second["title"]},
                ],
                selectedTaskId=second["id"],
            ),
            follow_up,
        )
        self.assertEqual(self.store.load()["pending"], [])
        state = self.store.load(projection=True)
        projection = self.store.projected_state(now_ms=1_002, state=state)
        self.assertEqual(
            projection.canonical_timer.get("taskId"), second["id"]
        )

    def test_lost_response_retries_byte_identical_payload(self) -> None:
        _first, _second, start, retarget = self._start_with_retarget()
        request = self.store.sync_payload()
        claimed = deepcopy(request)
        # The response is lost: the claim stays durable for exact retry.
        self.assertEqual(self.store.sync_payload(), claimed)
        self.assertEqual(self.store.pending_sync(), claimed)
        response = _canonical_response(self.store, request, revision=1)
        self.store.apply_sync(response, request)
        # Retry would have carried the identical bytes; the acked queues
        # are gone and nothing was rewritten.
        self.assertEqual(self.store.load()["pending"], [])
        self.assertIsNone(self.store.pending_sync())

    def test_restart_restores_claim_proof_and_head(self) -> None:
        _first, _second, start, retarget = self._start_with_retarget()
        request = self.store.sync_payload()
        claimed = deepcopy(request)
        self.store.close()
        self.store = Store(
            self.path,
            iroh_secret_store=self.secrets,
            shared_core=V2EmulatingSharedCore(),
        )
        self.assertEqual(self.store.pending_sync(), claimed)
        # Claimed IDs stay possibly-delivered across restart; the rest
        # keeps never-sent proof.
        self.assertEqual(self.store.delivery_proof()["commands"], [])
        self.assertIsNone(self.store.canonical_head())
        response = _canonical_response(self.store, claimed, revision=1)
        self.store.apply_sync(response, claimed)
        self.assertEqual(self.store.load()["pending"], [])
        self.assertEqual(
            self.store.canonical_head(),
            (claimed["commands"][-1]["hlcWallMs"], 0),
        )

    def test_already_synced_start_converges_retargeted_history(self) -> None:
        first, second, start, retarget = self._start_with_retarget()
        request = self.store.sync_payload()
        running_timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": second["id"],
            "startedByDeviceId": self.store.device_id,
            "lastIntent": {
                "type": "start",
                "commandId": start["id"],
                "occurredAt": start["occurredAt"],
            },
        }
        self.store.apply_sync(
            _canonical_response(
                self.store, request, revision=1,
                canonicalTimer=running_timer,
                tasks=[{"id": first["id"], "title": first["title"]},
                       {"id": second["id"], "title": second["title"]}],
                selectedTaskId=second["id"],
            ),
            request,
        )
        self.assertEqual(self.store.load()["pending"], [])
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": second["id"],
        }
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "finish", timer, "focus", settings["durationsMs"], now_ms=2_000
        )
        finish_request = self.store.sync_payload()
        finish = next(
            c for c in finish_request["commands"] if c["type"] == "finish"
        )
        history = [
            {
                "id": f"history-{start['timerId']}",
                "timerId": start["timerId"],
                "commandId": finish["id"],
                "phase": "focus",
                "status": "completed",
                "plannedDurationMs": start["plannedDurationMs"],
                "completedAt": finish["occurredAt"],
                "taskId": second["id"],
            }
        ]
        self.store.apply_sync(
            _canonical_response(
                self.store, finish_request, revision=2, history=history,
                tasks=[{"id": first["id"], "title": first["title"]},
                       {"id": second["id"], "title": second["title"]}],
            ),
            finish_request,
        )
        state = self.store.load(projection=True)
        projection = self.store.projected_state(now_ms=2_000, state=state)
        history_view = self.store.projected_history(projection, state)
        self.assertEqual(len(history_view), 1)
        # Convergence comes from Core, not a client overlay: the stored
        # history already carries the retargeted attribution.
        self.assertEqual(history_view[0].get("taskId"), second["id"])
        self.assertNotIn("pending", history_view[0])
        self.assertEqual(
            state["snapshot"]["history"][0].get("taskId"), second["id"]
        )

    def test_false_delivery_claim_is_rejected(self) -> None:
        core = V2EmulatingSharedCore()
        local = {
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        }
        sent = deepcopy(local)
        response = {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {"focus": 1_500_000, "short_break": 300_000,
                            "long_break": 900_000},
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(1_000),
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }
        with self.assertRaisesRegex(
            SharedCoreOperationError, "neverSent"
        ):
            core.dispatch(
                "reconcile.rebase.v2",
                {
                    "local": local,
                    "sent": sent,
                    "neverSent": {"commands": ["absent-command"]},
                    "response": response,
                    "timerDependencies": [],
                },
            )

    def test_client_never_claims_unknown_ids(self) -> None:
        _first, _second, _start, _retarget = self._start_with_retarget()
        request = self.store.sync_payload()
        proof = self.store.delivery_proof()
        proof["commands"].append("absent-command")
        self.store.set_meta("deliveryProof", proof)
        # Stale proof entries outside the local queues are filtered from
        # the claim instead of invalidating the round trip.
        self.store.apply_sync(
            _canonical_response(self.store, request, revision=1), request
        )
        self.assertEqual(self.store.load()["pending"], [])

    def test_queued_start_bytes_never_rewritten(self) -> None:
        first, second, start, _retarget = self._start_with_retarget()
        stored = self.store.connection.execute(
            "SELECT payload FROM pending_commands WHERE id = ?", (start["id"],)
        ).fetchone()["payload"]
        self.assertEqual(json.loads(stored), start)
        self.assertEqual(start.get("taskId"), first["id"])
        # Attribution changes queue new operations; the Start bytes stay put.
        before = stored
        self.store.set_selected_task_id(second["id"], now_ms=5_000)
        after = self.store.connection.execute(
            "SELECT payload FROM pending_commands WHERE id = ?", (start["id"],)
        ).fetchone()["payload"]
        self.assertEqual(after, before)

    def test_iroh_capture_publishes_exact_retarget_and_converges(self) -> None:
        import time

        first = task_from_title("Immut first")
        second = task_from_title("Immut second")
        room_id = self.store.create_iroh_room(bytes(range(32)))
        base_ms = int(time.time() * 1000)
        self.store.queue_task_operation("upsert", first, now_ms=base_ms)
        self.store.queue_task_operation("upsert", second, now_ms=base_ms + 1)
        self.store.set_selected_task_id(first["id"], now_ms=base_ms + 2)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], first["id"],
            now_ms=base_ms + 10,
        )
        start_id, start_timer_id = start["id"], start["timerId"]
        self.store.set_selected_task_id(second["id"], now_ms=base_ms + 11)
        # Iroh mode publishes each mutation immediately, draining the
        # queues; the exact published records carry the operations.
        rows = self.store.connection.execute(
            "SELECT domain, operation_id, digest, record FROM iroh_records "
            "WHERE room_id = ?",
            (room_id,),
        ).fetchall()
        by_id = {row["operation_id"]: row for row in rows}
        timer_records = [
            json.loads(row["record"])
            for row in rows
            if row["domain"] == "timer"
        ]
        retarget_records = [
            record for record in timer_records
            if record["operation"].get("type") == "retarget"
        ]
        self.assertEqual(len(retarget_records), 1)
        record = retarget_records[0]
        retarget = record["operation"]
        self.assertEqual(record["deviceId"], self.store.device_id)
        self.assertEqual(retarget["timerId"], start_timer_id)
        self.assertEqual(retarget["taskId"], second["id"])
        self.assertEqual(by_id[retarget["id"]]["digest"], record_digest(record))
        self.assertEqual(record_id(record), retarget["id"])
        start_records = [
            record for record in timer_records
            if record["operation"].get("type") == "start"
        ]
        self.assertEqual(len(start_records), 1)
        # The queued Start kept its original attribution: no same-ID rewrite.
        self.assertEqual(
            start_records[0]["operation"].get("taskId"), first["id"]
        )
        self.assertEqual(start_records[0]["operation"]["id"], start_id)
        # Publication retired proof and cleared queues atomically.
        self.assertEqual(self.store.load()["pending"], [])
        self.assertEqual(
            self.store.delivery_proof(),
            {domain: [] for domain in self.store.delivery_proof()},
        )
        room_view = self.store._replication_storage._projection.project_room(room_id)
        self.assertEqual(room_view["canonicalTimer"]["taskId"], second["id"])
        self.assertEqual(room_view["canonicalTimer"]["id"], start_timer_id)
        self.assertIn(first, room_view["knownTasks"] + room_view["tasks"])


class ProductionBundleV2Tests(unittest.TestCase):
    def test_apply_sync_uses_real_v2_for_task_upsert(self) -> None:
        from pomodorough.shared_core import SharedCore

        version = SharedCore().dispatch("core.version", {})
        self.assertEqual(version, {"schemaVersion": 1, "coreVersion": "0.38.0"})
        temporary = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temporary.name) / "state.sqlite3")
            try:
                store.queue_task_operation(
                    "upsert", task_from_title("Real v2 probe"), now_ms=1
                )
                request = store.sync_payload()
                store.apply_sync(
                    _canonical_response(store, request), request
                )
                self.assertEqual(store.load()["pending"], [])
                self.assertIsNone(store.pending_sync())
            finally:
                store.close()
        finally:
            temporary.cleanup()

    def test_unsupported_v2_bundle_still_fails_closed(self) -> None:
        from pomodorough.shared_core import SharedCoreOperationError

        temporary = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temporary.name) / "state.sqlite3")
            try:
                class UnsupportedCore:
                    @staticmethod
                    def dispatch(operation: str, value: object) -> object:
                        del value
                        raise SharedCoreOperationError(
                            operation,
                            "unsupported shared-core operation: "
                            "reconcile.rebase.v2",
                        )

                store.queue_task_operation(
                    "upsert", task_from_title("Stale bundle probe"), now_ms=1
                )
                request = store.sync_payload()
                before = store.load()
                # Install the stale bundle only around apply_sync: queueing
                # itself needs a working Core.
                store._shared_core = UnsupportedCore()  # type: ignore[assignment]
                with self.assertRaisesRegex(
                    ValueError, "upgrade pinned Core.*no v1 fallback"
                ):
                    store.apply_sync(
                        _canonical_response(store, request), request
                    )
                # No fallback to v1: queues and the claimed request survive
                # for recovery instead of a modified retry.
                self.assertEqual(store.load(), before)
                self.assertEqual(store.pending_sync(), request)
            finally:
                store.close()
        finally:
            temporary.cleanup()

    def test_production_dispatches_v2_and_never_v1(self) -> None:
        from pomodorough.shared_core import SharedCore

        temporary = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temporary.name) / "state.sqlite3")
            try:
                real = SharedCore()
                seen: list[str] = []

                class RecordingCore:
                    def dispatch(self, operation: str, value: object) -> object:
                        seen.append(operation)
                        return real.dispatch(operation, value)

                store._shared_core = RecordingCore()  # type: ignore[assignment]
                store.queue_task_operation(
                    "upsert", task_from_title("No fallback probe"), now_ms=1
                )
                request = store.sync_payload()
                seen.clear()
                store.apply_sync(
                    _canonical_response(store, request), request
                )
                self.assertIn("reconcile.rebase.v2", seen)
                self.assertNotIn("reconcile.rebase.v1", seen)
            finally:
                store.close()
        finally:
            temporary.cleanup()


class DoubleVsAuthoritativeTests(unittest.TestCase):
    """The double emulates delivery; Core stays authoritative for policy."""

    def test_double_is_test_only_and_delegates_to_real_core(self) -> None:
        from pomodorough.shared_core import SharedCore

        core = V2EmulatingSharedCore()
        self.assertIsInstance(core.delegate, SharedCore)
        self.assertTrue(getattr(core, "TEST_ONLY_DOUBLE", False))
        temporary = tempfile.TemporaryDirectory()
        try:
            store = Store(Path(temporary.name) / "state.sqlite3")
            try:
                self.assertIsNone(store._shared_core)
            finally:
                store.close()
        finally:
            temporary.cleanup()

    def test_double_serves_v2_alongside_real_core(self) -> None:
        from pomodorough.shared_core import SharedCore

        version = SharedCore().dispatch("core.version", {})
        self.assertEqual(version, {"schemaVersion": 1, "coreVersion": "0.38.0"})
        core = V2EmulatingSharedCore()
        local = {
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [],
            "selectedTaskOperations": [],
        }
        response = {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [],
            "autoStartAcknowledgements": [],
            "selectedTaskAcknowledgements": [],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {"focus": 1_500_000, "short_break": 300_000,
                            "long_break": 900_000},
            "autoStartBreaks": False,
            "selectedTaskId": None,
            "serverTime": utc_timestamp(1_000),
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }
        result = core.dispatch(
            "reconcile.rebase.v2",
            {"local": local, "sent": deepcopy(local),
             "response": response, "timerDependencies": []},
        )
        self.assertIsInstance(result, dict)


class LegacyTaskRetargetsCleanupTests(unittest.TestCase):
    def test_legacy_marker_is_dropped_on_open(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            path = Path(temporary.name) / "state.sqlite3"
            store = Store(
                path, iroh_secret_store=MemorySecretStore(),
                shared_core=V2EmulatingSharedCore(),
            )
            store.set_meta("taskRetargets", {"timer-1": "task-1"})
            store.close()
            reopened = Store(
                path, iroh_secret_store=MemorySecretStore(),
                shared_core=V2EmulatingSharedCore(),
            )
            try:
                raw = reopened.connection.execute(
                    "SELECT value FROM meta WHERE key = 'taskRetargets'"
                ).fetchone()
                self.assertIsNone(raw)
            finally:
                reopened.close()
        finally:
            temporary.cleanup()


class RetargetCapabilityGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.secrets = MemorySecretStore()
        self.store = Store(
            self.path, iroh_secret_store=self.secrets,
            shared_core=V2EmulatingSharedCore(),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _start_and_select(self, base_ms: int | None = None) -> tuple[str, str]:
        import time as _time

        base = base_ms if base_ms is not None else int(_time.time() * 1000)
        first = task_from_title("Gate first")
        second = task_from_title("Gate second")
        self.store.queue_task_operation("upsert", first, now_ms=base)
        self.store.queue_task_operation("upsert", second, now_ms=base + 1)
        self.store.set_selected_task_id(first["id"], now_ms=base + 2)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], first["id"],
            now_ms=base + 10,
        )
        return start["timerId"], second["id"]

    def test_peer_capabilities_round_trip_and_validation(self) -> None:
        room_id = self.store.create_iroh_room(bytes(range(32)))
        self.store.upsert_iroh_peer(
            room_id, "peer-new", "ticket-new", None, None,
            capabilities=["retarget-v1"],
        )
        peer = next(
            item for item in self.store.iroh_peers(room_id)
            if item["endpointId"] == "peer-new"
        )
        self.assertEqual(peer["capabilities"], ["retarget-v1"])
        self.assertTrue(self.store.iroh_peers_support(room_id, "retarget-v1"))
        with self.assertRaisesRegex(ValueError, "capabilities are invalid"):
            self.store.upsert_iroh_peer(
                room_id, "peer-bad", "ticket-bad", None, None,
                capabilities=["retarget-v1", "retarget-v1"],
            )

    def test_legacy_peer_holds_back_retarget_until_upgrade(self) -> None:
        import time as _time

        room_id = self.store.create_iroh_room(bytes(range(32)))
        _timer_id, second_id = self._start_and_select()
        self.assertEqual(self.store.load()["pending"], [])
        self.store.upsert_iroh_peer(
            room_id, "peer-legacy", "ticket-legacy", None, None,
        )
        self.assertFalse(
            self.store.iroh_peers_support(room_id, "retarget-v1")
        )
        self.store.set_selected_task_id(
            second_id, now_ms=int(_time.time() * 1000) + 20
        )
        pending = self.store.load()["pending"]
        retargets = [c for c in pending if c.get("type") == "retarget"]
        self.assertEqual(len(retargets), 1)
        rows = self.store.connection.execute(
            "SELECT operation_id FROM iroh_records WHERE room_id = ? "
            "AND domain = 'timer'",
            (room_id,),
        ).fetchall()
        published = {str(row["operation_id"]) for row in rows}
        self.assertNotIn(retargets[0]["id"], published)
        self.store.upsert_iroh_peer(
            room_id, "peer-legacy", "ticket-legacy", None, None,
            capabilities=["retarget-v1"],
        )
        self.assertTrue(
            self.store.iroh_peers_support(room_id, "retarget-v1")
        )
        self.store._replication_storage.capture_local_iroh_records()
        self.assertEqual(self.store.load()["pending"], [])
        rows = self.store.connection.execute(
            "SELECT operation_id FROM iroh_records WHERE room_id = ? "
            "AND domain = 'timer'",
            (room_id,),
        ).fetchall()
        published = {str(row["operation_id"]) for row in rows}
        self.assertIn(retargets[0]["id"], published)

    def test_mixed_room_publishes_non_retarget_while_holding_retarget(
        self,
    ) -> None:
        import time as _time

        room_id = self.store.create_iroh_room(bytes(range(32)))
        _timer_id, second_id = self._start_and_select()
        self.store.upsert_iroh_peer(
            room_id, "peer-legacy", "ticket-legacy", None, None,
        )
        base = int(_time.time() * 1000)
        extra = task_from_title("Gate extra")
        self.store.queue_task_operation("upsert", extra, now_ms=base + 30)
        self.store.set_selected_task_id(second_id, now_ms=base + 40)
        self.store._replication_storage.capture_local_iroh_records()
        pending = self.store.load()["pending"]
        self.assertEqual([c.get("type") for c in pending], ["retarget"])
        task_rows = self.store.connection.execute(
            "SELECT operation_id FROM iroh_records WHERE room_id = ? "
            "AND domain = 'task'",
            (room_id,),
        ).fetchall()
        self.assertTrue(len(task_rows) >= 1)


if __name__ == "__main__":
    unittest.main()
