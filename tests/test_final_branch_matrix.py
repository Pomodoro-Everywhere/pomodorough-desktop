from __future__ import annotations

import copy
import tempfile
import unittest
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from iroh_protocol_cases import DEVICE_ID, TIMESTAMP, TIMESTAMP_MS, genesis_record

from pomodorough.core import task_from_title
from pomodorough.iroh_network import IrohService
from pomodorough.iroh_protocol import (
    IrohProtocolError,
    RoomInvite,
    _inventory_entry_key,
    _valid_peer_clock,
    _validate_genesis_intent,
    validate_record,
)
from pomodorough.network_account import (
    AccountDeletionCredentials,
    AccountLifecycle,
    RevocationState,
)
from pomodorough.network_session import ApiError, SessionState
from pomodorough.shared_core import (
    ProjectionApplyV2,
    ProjectionWinningOperationIds,
    SharedCore,
    SharedCoreABIError,
    SharedCoreError,
    _DispatchBuffers,
    _exact_object,
    _operation_winner,
    _read_packaged_wasm,
    _validated_duration_winners,
    _validated_scalar_winner,
    _validated_task_winners,
)
from pomodorough.storage import MAX_CLOCK_SKEW_MS, Store, utc_timestamp
from pomodorough.storage_sync import SyncStorage
from pomodorough.terminal import InvalidAction, LocalTimer

NOW = datetime(2026, 8, 25, tzinfo=UTC)


class AccountLifecycleBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = SessionState()
        self.tokens = Mock()
        self.request = Mock()
        self.lifecycle = AccountLifecycle(
            "https://example.test",
            self.state,
            self.tokens,
            self.request,
            lambda key: key,
            lambda: NOW,
        )

    def test_deletion_uses_fresh_token_and_rejects_missing_refresh_or_invalid_response(self) -> None:
        fresh = AccountDeletionCredentials("fresh", NOW + timedelta(minutes=2), "refresh")
        self.assertEqual(self.lifecycle._deletion_access(fresh), "fresh")
        self.request.assert_not_called()

        missing = AccountDeletionCredentials(None, NOW, None)
        with self.assertRaisesRegex(ApiError, "sign_in_required"):
            self.lifecycle._deletion_access(missing)

        expired = AccountDeletionCredentials("expired", NOW, "refresh")
        for response in ({}, {"accessToken": "   "}):
            with self.subTest(response=response):
                self.request.return_value = response
                with self.assertRaisesRegex(ApiError, "invalid_token"):
                    self.lifecycle.refresh_deletion_access(expired)

    def test_revocation_retries_unauthorized_access_with_rotated_refresh(self) -> None:
        revocation = RevocationState("expired", "refresh-one", True)
        self.request.side_effect = [ApiError("expired", 401), {
            "accessToken": "fresh-access",
            "refreshToken": "refresh-two",
        }, {}]

        self.lifecycle.revoke(revocation)

        self.assertEqual(
            [call.kwargs.get("access_token") for call in self.request.call_args_list],
            ["expired", None, "fresh-access"],
        )
        self.assertEqual(revocation.refresh_token, "refresh-two")
        self.assertTrue(revocation.access_token_is_fresh)

    def test_revocation_preserves_non_auth_failure_and_requires_refresh(self) -> None:
        for status, refresh in ((503, "refresh"), (401, None)):
            self.request.reset_mock(side_effect=True)
            self.request.side_effect = ApiError("failure", status)
            with self.subTest(status=status, refresh=refresh), self.assertRaisesRegex(
                ApiError, "failure"
            ):
                self.lifecycle.revoke(RevocationState("access", refresh, True))

        with self.assertRaisesRegex(ApiError, "sign_in_required"):
            self.lifecycle._revocation_access(RevocationState(None, None, False))

    def test_revocation_refresh_rejects_invalid_access_and_keeps_refresh_when_not_rotated(self) -> None:
        revocation = RevocationState(None, "captured-refresh", False)
        for response in ({}, {"accessToken": ""}):
            self.request.return_value = response
            with self.subTest(response=response), self.assertRaisesRegex(
                ApiError, "invalid_token"
            ):
                self.lifecycle.refresh_revocation_access(revocation)

        self.request.return_value = {"accessToken": "fresh", "refreshToken": ""}
        self.assertEqual(self.lifecycle.refresh_revocation_access(revocation), "fresh")
        self.assertEqual(revocation.refresh_token, "captured-refresh")
        self.assertTrue(revocation.access_token_is_fresh)


class SyncStoreStub:
    def __init__(self) -> None:
        self.device_id = "device-matrix-0001"
        self.connection = SimpleNamespace(in_transaction=False)
        self.values: dict[str, object] = {}
        self.inside: list[tuple[str, object]] = []
        self.outside: list[tuple[str, object]] = []

    def get_meta(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def _set_meta(self, key: str, value: object) -> None:
        self.values[key] = value
        self.inside.append((key, value))

    def set_meta(self, key: str, value: object) -> None:
        self.values[key] = value
        self.outside.append((key, value))

    @staticmethod
    def _bounded_integer(value: object, _label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("bad integer")
        return value


class SyncStorageBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SyncStoreStub()
        self.sync = SyncStorage(self.store)  # type: ignore[arg-type]

    def test_wire_preferences_strip_local_device_and_reject_corrupt_collections(self) -> None:
        operation = {"id": "operation-1", "deviceId": DEVICE_ID, "enabled": True}
        self.assertEqual(
            self.sync._wire_preference_operations([operation], "corrupt"),
            [{"id": "operation-1", "enabled": True}],
        )
        self.assertIn("deviceId", operation)
        for value in ({}, [operation, "bad"]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "corrupt"):
                self.sync._wire_preference_operations(value, "corrupt")

    def test_meta_replacement_respects_existing_transaction_boundary(self) -> None:
        self.sync._replace_meta_inside_or_outside_transaction("claim", {"id": 1})
        self.assertEqual(self.store.outside, [("claim", {"id": 1})])
        self.store.connection.in_transaction = True
        self.sync._replace_meta_inside_or_outside_transaction("claim", {"id": 2})
        self.assertEqual(self.store.inside, [("claim", {"id": 2})])

    def test_pending_sync_upgrades_legacy_claim_and_rejects_wrong_owner_or_queue_shape(self) -> None:
        legacy = {
            "deviceId": self.store.device_id,
            "lastRevision": 4,
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
            "autoStartOperations": [{"id": "a", "deviceId": DEVICE_ID}],
        }
        self.store.values["pendingSync"] = legacy
        upgraded = self.sync.pending_sync()
        self.assertEqual(upgraded["selectedTaskOperations"], [])
        self.assertNotIn("deviceId", upgraded["autoStartOperations"][0])
        self.assertEqual(self.store.outside[-1][0], "pendingSync")

        corruptions = (
            {**legacy, "deviceId": "other-device"},
            {**legacy, "commands": {}},
            ["not", "an", "object"],
        )
        for pending in corruptions:
            with self.subTest(pending=pending):
                self.store.values["pendingSync"] = pending
                with self.assertRaisesRegex(ValueError, "claim is corrupted"):
                    self.sync.pending_sync()

    def test_pending_resolution_hides_other_account_after_validating_and_normalizing(self) -> None:
        pending = {
            "owner": {"id": "user-one"},
            "request": {
                "requestId": "request-one",
                "deviceId": self.store.device_id,
                "strategy": "merge",
                "autoStartOperations": [{"id": "a", "deviceId": DEVICE_ID}],
            },
            "queueIds": {
                "commands": [],
                "taskOperations": [],
                "durationOperations": [],
                "autoStartOperations": [],
            },
        }
        self.store.values["pendingResolution"] = pending
        self.assertIsNone(self.sync.pending_resolution("user-two"))
        normalized = self.store.outside[-1][1]
        self.assertNotIn("deviceId", normalized["request"]["autoStartOperations"][0])

    def test_bootstrap_plan_validation_covers_choose_counts_and_auto_pairs(self) -> None:
        completed = [{"id": "history-one", "timerId": "timer-one", "status": "completed"}]
        choose = {"mode": "choose", "localHistoryCount": 1, "remoteHistoryCount": 0}
        self.assertIsNone(SyncStorage._validated_bootstrap_plan(choose, completed, []))
        invalid = (
            [],
            {**choose, "extra": True},
            {**choose, "localHistoryCount": True},
            {**choose, "localHistoryCount": 0},
            {"mode": "auto", "strategy": "merge", "reason": "remote_only"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid bootstrap plan"
            ):
                SyncStorage._validated_bootstrap_plan(value, completed, [])
        self.assertEqual(
            SyncStorage._validated_bootstrap_plan(
                {"mode": "auto", "strategy": "merge", "reason": "local_state_only"},
                [],
                [],
            ),
            "merge",
        )

    def test_resolution_identity_rejects_strategy_revision_and_identity_independently(self) -> None:
        cases = (
            ({"id": "user"}, 1, "unknown", "Unsupported"),
            ({"id": "user"}, True, "merge", "invalid revision"),
            ({"id": "user"}, -1, "merge", "invalid revision"),
            ({}, 1, "merge", "stable identity"),
        )
        for user, revision, strategy, message in cases:
            with self.subTest(case=(user, revision, strategy)), self.assertRaisesRegex(
                ValueError, message
            ):
                SyncStorage._validated_resolution_identity(user, revision, strategy)
        self.assertEqual(
            SyncStorage._validated_resolution_identity({"id": "user"}, 1, "merge"),
            "user",
        )


class SharedCoreBoundaryTests(unittest.TestCase):
    def test_projection_requires_explicit_applied_outcome(self) -> None:
        projection = ProjectionApplyV2(
            None,
            [],
            [],
            {},
            False,
            None,
            {"applied": {"outcome": "applied"}, "ignored": {"outcome": "ignored"}},
            ProjectionWinningOperationIds({}, {}, None, None),
        )
        projection.require_applied("applied")
        for command in ("ignored", "missing"):
            with self.subTest(command=command), self.assertRaisesRegex(
                SharedCoreABIError, command
            ):
                projection.require_applied(command)

    def test_dispatch_result_and_input_boundaries_fail_before_unsafe_memory_access(self) -> None:
        with self.assertRaisesRegex(SharedCoreABIError, "did not return an i64"):
            _DispatchBuffers(b"operation", b"{}")._capture_result(Mock(), "bad")

        core = SharedCore()
        with self.assertRaisesRegex(ValueError, "input is too large"):
            core.dispatch_json("core.version", "x" * (16 * 1024 * 1024 + 1))

    def test_dispatch_failure_classifies_host_and_abi_errors_without_masking_domain_errors(self) -> None:
        domain = SharedCoreError("domain")
        self.assertIs(SharedCore._dispatch_failure(domain), domain)
        abi = SharedCore._dispatch_failure(IndexError("memory"))
        self.assertIsInstance(abi, SharedCoreABIError)
        self.assertIsInstance(abi.__cause__, IndexError)
        ordinary = RuntimeError("ordinary")
        self.assertIs(SharedCore._dispatch_failure(ordinary), ordinary)

    def test_cleanup_failure_preserves_primary_and_reports_all_cleanup_errors(self) -> None:
        core = SharedCore.__new__(SharedCore)
        core._unusable_cause = None
        primary = ValueError("primary")
        cleanup = [OSError("first"), RuntimeError("second")]
        with self.assertRaisesRegex(ValueError, "primary") as raised:
            core._complete_dispatch(None, primary, cleanup)
        self.assertEqual(len(raised.exception.__notes__), 2)
        self.assertIs(core._unusable_cause, cleanup[0])

        core._unusable_cause = None
        with self.assertRaisesRegex(SharedCoreABIError, "cleanup failed") as raised_cleanup:
            core._complete_dispatch(None, None, cleanup)
        self.assertIn("additional cleanup", raised_cleanup.exception.__notes__[0])
        self.assertIs(raised_cleanup.exception.__cause__, cleanup[0])

    def test_allocator_rejects_invalid_pointer_and_incomplete_write(self) -> None:
        core = SharedCore.__new__(SharedCore)
        core._store = object()
        core._require_range = Mock()
        core._release = Mock()
        core._memory = Mock(write=Mock(return_value=2))

        for raw, message in (("bad", "did not return"), (0, "null pointer")):
            core._allocate_export = Mock(return_value=raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(SharedCoreABIError, message):
                core._allocate(b"abc")

        core._allocate_export = Mock(return_value=10)
        with self.assertRaisesRegex(SharedCoreABIError, "write was incomplete"):
            core._allocate(b"abc")
        core._release.assert_called_once_with(10, 3)

    def test_memory_range_and_export_types_fail_closed(self) -> None:
        core = SharedCore.__new__(SharedCore)
        core._store = object()
        core._memory = Mock(data_len=Mock(return_value=16))
        for pointer, length in ((-1, 1), (15, 2)):
            with self.subTest(pointer=pointer, length=length), self.assertRaisesRegex(
                SharedCoreABIError, "outside linear memory"
            ):
                core._require_range(pointer, length, "test")

        core._export = Mock(return_value=object())
        with self.assertRaisesRegex(SharedCoreABIError, "is not memory"):
            core._require_memory("memory")
        with self.assertRaisesRegex(SharedCoreABIError, "is not a function"):
            core._require_func("dispatch", (), ())

        live = SharedCore()
        live._export = Mock(return_value=live._dispatch_export)
        with self.assertRaisesRegex(SharedCoreABIError, "has type"):
            live._require_func("dispatch", (), ())

    def test_envelope_and_exact_object_reject_non_object_or_non_string_keys(self) -> None:
        for document in ("[]", '{"ok":1}'):
            with self.subTest(document=document), self.assertRaisesRegex(
                SharedCoreABIError, "boolean ok"
            ):
                SharedCore._parse_envelope("operation", document)
        with self.assertRaisesRegex(SharedCoreABIError, "non-string field"):
            _exact_object({1: "value"}, {1}, label="matrix")  # type: ignore[arg-type]

    def test_projection_winner_validators_reject_inconsistent_identity_and_value(self) -> None:
        task_operation = {
            "id": "task-operation",
            "taskId": "task-id",
            "type": "upsert",
            "title": "Title",
            "hlcWallMs": 1,
            "hlcCounter": 0,
            "deviceId": "device-id",
        }
        with self.assertRaisesRegex(SharedCoreABIError, "task winner"):
            _validated_task_winners(
                {"task-id": "wrong"},
                {"task-id": [task_operation]},
                {"task-id": {"id": "task-id", "title": "Title"}},
            )
        duration = {
            "id": "duration-operation",
            "phase": "focus",
            "durationMs": 60_000,
            "hlcWallMs": 1,
            "hlcCounter": 0,
            "deviceId": "device-id",
        }
        with self.assertRaisesRegex(SharedCoreABIError, "duration winner"):
            _validated_duration_winners(
                {"focus": "duration-operation"}, {"focus": [duration]}, {"focus": 120_000}
            )
        self.assertIsNone(_operation_winner([], "empty"))

    def test_scalar_winner_rejects_wrong_winner_and_wrong_projected_value(self) -> None:
        operation = {
            "id": "selection-operation",
            "taskId": "task-id",
            "hlcWallMs": 1,
            "hlcCounter": 0,
            "deviceId": "device-id",
        }
        operations = {"selectedTaskOperations": [operation]}
        winners = {"selectedTask": "wrong"}
        with self.assertRaisesRegex(SharedCoreABIError, "winner is inconsistent"):
            _validated_scalar_winner(
                winners, operations, "selectedTask", "selectedTaskOperations",
                "task-id", "taskId", {"task-id": {"id": "task-id"}},
            )
        winners["selectedTask"] = "selection-operation"
        with self.assertRaisesRegex(SharedCoreABIError, "value is inconsistent"):
            _validated_scalar_winner(
                winners, operations, "selectedTask", "selectedTaskOperations",
                "other-task", "taskId", {"task-id": {"id": "task-id"}},
            )

    def test_packaged_manifest_rejects_commit_and_checksum_mismatch(self) -> None:
        class Resource:
            def __init__(self, values: dict[str, object], name: str = "resources") -> None:
                self.values = values
                self.name = name

            def joinpath(self, name: str) -> Resource:
                return Resource(self.values, name)

            def read_text(self, encoding: str) -> str:
                del encoding
                return str(self.values[self.name])

            def read_bytes(self) -> bytes:
                return bytes(self.values[self.name])

        values = {
            "CORE_COMMIT": "wrong",
            "pomodorough_core.wasm.sha256": "wrong manifest",
            "pomodorough_core.wasm": b"wasm",
        }
        with patch("pomodorough.shared_core.files", return_value=Resource(values)), self.assertRaisesRegex(
            SharedCoreError, "commit mismatch"
        ):
            _read_packaged_wasm()
        values["CORE_COMMIT"] = "49efee8c5ac390d5dd7bd5c1a3537fb889fa6f10"
        with patch("pomodorough.shared_core.files", return_value=Resource(values)), self.assertRaisesRegex(
            SharedCoreError, "manifest is invalid"
        ):
            _read_packaged_wasm()


class IrohProtocolBoundaryTests(unittest.TestCase):
    @staticmethod
    def _timer() -> dict[str, object]:
        return {
            "id": "timer-identity-0001",
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 1_500_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": TIMESTAMP,
            "taskId": "task-identity-0001",
            "startedByDeviceId": DEVICE_ID,
            "lastIntent": {
                "type": "start",
                "commandId": "command-identity-0001",
                "occurredAt": TIMESTAMP,
            },
        }

    @staticmethod
    def _history(item_id: str = "history-identity-0001") -> dict[str, object]:
        return {
            "id": item_id,
            "timerId": "timer-history-0001",
            "commandId": "command-history-0001",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 1_500_000,
            "completedAt": TIMESTAMP,
            "endedAt": TIMESTAMP,
        }

    def test_room_invite_refuses_ticket_identity_change(self) -> None:
        invite = RoomInvite("room", "ticket", "expected", bytes(32))
        with patch("pomodorough.iroh_protocol._ticket_endpoint_id", return_value="changed"), self.assertRaisesRegex(
            IrohProtocolError, "identity changed"
        ):
            invite.encode()

    def test_peer_clock_distinguishes_legacy_sentinel_from_invalid_zero_clock(self) -> None:
        sentinel = {"occurredAt": "1970-01-01T00:00:00.000Z", "hlcWallMs": 0, "hlcCounter": 0}
        _valid_peer_clock(sentinel, allow_zero=True)
        with self.assertRaisesRegex(IrohProtocolError, "sentinel"):
            _valid_peer_clock(sentinel)
        with self.assertRaisesRegex(IrohProtocolError, "sentinel"):
            _valid_peer_clock({**sentinel, "occurredAt": TIMESTAMP}, allow_zero=True)

    def test_genesis_accepts_canonical_timer_intent_history_and_tasks(self) -> None:
        record = genesis_record()
        task = task_from_title("Canonical task")
        timer = self._timer()
        timer["taskId"] = task["id"]
        record["operation"].update(
            canonicalTimer=timer,
            history=[self._history()],
            tasks=[task],
            selectedTaskId=task["id"],
        )
        self.assertIs(validate_record(record), record)

    def test_genesis_timer_rejects_invalid_identity_and_non_object_intent(self) -> None:
        for mutation, message in (
            (("id", "bad"), "timer identity"),
            (("taskId", "bad"), "timer identity"),
            (("lastIntent", "bad"), "canonical timer"),
        ):
            timer = self._timer()
            timer[mutation[0]] = mutation[1]
            record = genesis_record()
            record["operation"]["canonicalTimer"] = timer
            with self.subTest(mutation=mutation), self.assertRaisesRegex(IrohProtocolError, message):
                validate_record(record)
        with self.assertRaisesRegex(IrohProtocolError, "intent is invalid"):
            _validate_genesis_intent("bad")

    def test_genesis_intent_rejects_local_origin_and_invalid_command_identity(self) -> None:
        intents = (
            {"type": "start", "commandId": "command-identity-0001", "occurredAt": TIMESTAMP, "deviceId": DEVICE_ID},
            {"type": "start", "commandId": "bad", "occurredAt": TIMESTAMP},
        )
        for intent in intents:
            record = genesis_record()
            timer = self._timer()
            timer["lastIntent"] = intent
            record["operation"]["canonicalTimer"] = timer
            with self.subTest(intent=intent), self.assertRaises(IrohProtocolError):
                validate_record(record)

    def test_genesis_history_rejects_invalid_optional_identity_null_and_duplicates(self) -> None:
        invalid_identity = self._history()
        invalid_identity["commandId"] = "bad"
        null_optional = self._history()
        null_optional["taskId"] = None
        duplicate = self._history()
        cases = ([invalid_identity], [null_optional], [duplicate, copy.deepcopy(duplicate)])
        for history in cases:
            record = genesis_record()
            record["operation"]["history"] = history
            with self.subTest(history=history), self.assertRaises(IrohProtocolError):
                validate_record(record)

    def test_genesis_tasks_reject_duplicate_identity(self) -> None:
        task = task_from_title("Duplicate task")
        record = genesis_record()
        record["operation"]["tasks"] = [task, copy.deepcopy(task)]
        with self.assertRaisesRegex(IrohProtocolError, "duplicate IDs"):
            validate_record(record)

    def test_non_genesis_rejects_operation_identity_before_domain_validation(self) -> None:
        record = {
            "domain": "selectedTask",
            "deviceId": DEVICE_ID,
            "operation": {
                "id": "bad",
                "taskId": None,
                "occurredAt": TIMESTAMP,
                "hlcWallMs": TIMESTAMP_MS,
                "hlcCounter": 0,
            },
        }
        with self.assertRaisesRegex(IrohProtocolError, "Operation ID"):
            validate_record(record)
        record["operation"]["id"] = "selected-operation-0001"
        record["operation"]["taskId"] = "bad"
        with self.assertRaisesRegex(IrohProtocolError, "Selected-task"):
            validate_record(record)

    def test_inventory_entry_rejects_non_object_and_invalid_reference(self) -> None:
        for entry in ([], {"domain": "unknown", "id": "operation-1", "digest": "x"}):
            with self.subTest(entry=entry), self.assertRaises(IrohProtocolError):
                _inventory_entry_key(entry, None, set())


class StorageAndTerminalBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "matrix.sqlite3")
        self.timer = LocalTimer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_response_timing_rejects_partial_and_backwards_monotonic_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.store._clock_sample_for_response(100, 90, None, 10, 20)
        with self.assertRaisesRegex(ValueError, "timing is invalid"):
            self.store._validated_response_timing(100, (90, 100, 20, 10))

    def test_running_timer_with_unparseable_anchor_falls_back_to_physical_time(self) -> None:
        timer = {
            "id": "timer-id",
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": "bad",
        }
        self.assertEqual(
            self.store.effective_timer_now_ms(timer, physical_ms=100, monotonic_ms=50),
            100,
        )

    def test_trusted_time_covers_offset_projection_discontinuity_and_anchor_rollback(self) -> None:
        sample = {
            "offsetMs": 10,
            "uncertaintyMs": 1,
            "acquiredPhysicalMs": 100,
            "acquiredMonotonicMs": 100,
            "acquiredTrustedMs": 110,
        }
        self.assertEqual(
            self.store._trusted_now_ms(200, use_monotonic=False, sample=sample), 210
        )
        with patch("pomodorough.storage.time.monotonic_ns", return_value=99_000_000):
            self.store._trusted_time_anchor = None
            self.assertEqual(self.store._trusted_now_ms(200, sample=sample), 200)
        self.store._trusted_time_anchor = sample
        with patch("pomodorough.storage.time.monotonic_ns", return_value=99_000_000):
            self.assertEqual(self.store._trusted_now_ms(200, sample=sample), 200)

    def test_pending_operation_clock_rejects_excessive_skew(self) -> None:
        occurred = 1_000_000
        operation = {
            "occurredAt": utc_timestamp(occurred),
            "hlcWallMs": occurred + MAX_CLOCK_SKEW_MS + 1,
            "hlcCounter": 0,
        }
        with self.assertRaisesRegex(ValueError, "trusted-time limit"):
            Store._operation_clock(operation)

    def test_timer_command_validation_rejects_unknown_action_and_missing_timer(self) -> None:
        settings = self.store.load()["settings"]
        arguments = (None, "focus", settings["durationsMs"], None, 1, 1, 1, (1, 0), "command-id", None)
        with self.assertRaisesRegex(ValueError, "Unsupported timer command"):
            self.store._prepare_timer_command("explode", *arguments)
        with self.assertRaisesRegex(ValueError, "No timer"):
            self.store._prepare_timer_command("pause", *arguments)

    def test_terminal_rejects_unknown_action_and_duration_bounds(self) -> None:
        with self.assertRaisesRegex(InvalidAction, "Unknown"):
            self.timer._validated_command("explode", {"status": "idle"})
        for minutes in (0, 181):
            with self.subTest(minutes=minutes), self.assertRaises(InvalidAction):
                self.timer._command_settings("focus", minutes)

    def test_primary_routes_running_paused_idle_and_terminal_states(self) -> None:
        for status, expected in (("running", "pause"), ("paused", "resume"), ("idle", "start")):
            with (
                self.subTest(status=status),
                patch.object(self.timer, "reload"),
                patch.object(
                    self.timer, "current_timer", return_value={"status": status}
                ),
                patch.object(self.timer, "issue") as issue,
            ):
                self.timer.primary(now_ms=5)
                issue.assert_called_once_with(expected, now_ms=5)

        self.timer.timer = {"id": "timer", "status": "completed"}
        self.timer.settings = {"durationsMs": {"focus": 60_000}, "selectedTaskId": None, "selectedPhase": "focus"}
        with patch.object(self.timer, "reload"), patch.object(
            self.store, "queue_restart", return_value=[{"type": "clear"}, {"type": "start"}]
        ) as restart:
            result = self.timer.primary(now_ms=5)
        self.assertEqual(result["type"], "start")
        restart.assert_called_once()

    def test_select_phase_blocks_active_timer_and_duration_adjustment_stops_at_bounds(self) -> None:
        with patch.object(self.timer, "reload"), patch.object(
            self.timer, "current_timer", return_value={"status": "running"}
        ), self.assertRaisesRegex(InvalidAction, "active"):
            self.timer.select_phase("short")

        self.timer.settings = {
            "selectedPhase": "focus",
            "durations": {"focus": 180},
            "durationsMs": {"focus": 10_800_000},
        }
        with patch.object(self.timer, "reload"), patch.object(
            self.store, "queue_duration_operation"
        ) as queue:
            self.assertEqual(self.timer.adjust_duration(1), 180)
        queue.assert_not_called()


class IrohServiceFinalBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = IrohService(Path("unused.sqlite3"), DEVICE_ID)

    def test_stop_with_worker_cancels_operations_and_submits_endpoint_shutdown(self) -> None:
        self.service._loop = Mock()
        self.service._cancel_operations = Mock()
        captured: list[object] = []

        def submit(coroutine: object, *, tracked: bool = False) -> None:
            self.assertFalse(tracked)
            captured.append(coroutine)

        self.service._submit = submit  # type: ignore[method-assign]
        self.service.stop()
        self.service._cancel_operations.assert_called_once()
        self.assertEqual(len(captured), 1)
        captured[0].close()

    def test_untracked_submit_reports_worker_failure_but_not_cancellation(self) -> None:
        loop = Mock()
        self.service._ensure_loop = Mock(return_value=loop)
        future: Future[object] = Future()
        failures: list[str] = []
        statuses: list[str] = []
        self.service.failure.connect(failures.append)
        self.service.status_changed.connect(statuses.append)

        async def operation() -> None:
            return None

        coroutine = operation()
        with patch(
            "pomodorough.iroh_network.asyncio.run_coroutine_threadsafe",
            return_value=future,
        ):
            returned = self.service._submit(coroutine, tracked=False)
        self.assertIs(returned, future)
        self.assertNotIn(future, self.service._operations)
        future.set_exception(RuntimeError("worker failed"))
        coroutine.close()
        self.assertEqual((statuses, failures), (["UNAVAILABLE"], ["worker failed"]))

    def test_details_include_peers_and_ready_status_distinguishes_routes(self) -> None:
        self.service._room_id = "room-identifier"
        self.service._store = Mock(
            iroh_room=Mock(return_value={"roomName": "Matrix"}),
            iroh_peers=Mock(return_value=[{"endpointId": "peer"}]),
        )
        details: list[object] = []
        statuses: list[str] = []
        self.service.details_changed.connect(details.append)
        self.service.status_changed.connect(statuses.append)
        self.service._emit_details()
        self.assertEqual(details, [{"roomName": "Matrix", "peers": [{"endpointId": "peer"}]}])

        self.service._emit_ready_status()
        self.assertEqual(statuses, [])
        identity = SimpleNamespace(fmt_short=lambda: "abcd")
        self.service._endpoint = SimpleNamespace(id=lambda: identity)
        self.service._relay_ready = False
        self.service._emit_ready_status()
        self.service._relay_ready = True
        self.service._emit_ready_status()
        self.assertEqual(statuses, ["DIRECT ROUTE · RELAY WAITING · ABCD", "READY FOR PEERS · ABCD"])


class IrohServiceFinalAsyncBranchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = IrohService(Path("unused.sqlite3"), DEVICE_ID)
        self.service._room_id = "room-identifier"
        self.service._room_secret = bytes(range(32))
        self.service._endpoint_ticket = "endpoint-ticket"

    async def test_refresh_route_does_not_emit_invite_when_none_was_requested(self) -> None:
        self.service._endpoint = SimpleNamespace(online=AsyncMock())
        self.service._generation = 3
        self.service._relay_ready = False
        self.service._invite_requested = False
        self.service._current_endpoint_ticket = Mock(return_value="fresh-ticket")
        self.service._emit_ready_status = Mock()
        self.service._emit_invite = AsyncMock()

        await self.service._refresh_online_route(3)

        self.assertTrue(self.service._relay_ready)
        self.service._emit_invite.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
