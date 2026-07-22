from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Thread
from unittest.mock import patch

from pomodorough.core import (
    project_auto_start_breaks,
    rebuild_optimistic,
    rebuild_tasks,
    task_from_title,
)
from pomodorough.storage import Store, default_data_path


class StorageTests(unittest.TestCase):
    def test_default_data_path_uses_platform_data_directory(self) -> None:
        root = Path("platform-data")
        with patch("pomodorough.storage.user_data_path", return_value=root) as path:
            self.assertEqual(
                default_data_path(), root / "pomodorough.sqlite3"
            )
        path.assert_called_once_with("pomodorough", appauthor=False)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _history_item(item_id: str) -> dict[str, object]:
        return {
            "id": item_id,
            "timerId": f"timer-{item_id}",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 25 * 60_000,
            "completedAt": "2026-07-22T10:00:00.000Z",
        }

    @staticmethod
    def _canonical_response(
        request: dict[str, object],
        *,
        revision: int = 1,
        history: list[dict[str, object]] | None = None,
        tasks: list[dict[str, str]] | None = None,
        auto_start_breaks: bool | None = None,
    ) -> dict[str, object]:
        auto_start_operations = request.get("autoStartOperations", [])
        if auto_start_breaks is None:
            auto_start_breaks = (
                auto_start_operations[-1]["enabled"]
                if auto_start_operations
                else False
            )
        return {
            "acknowledgements": [
                {"commandId": item["id"], "outcome": "applied", "reason": ""}
                for item in request["commands"]
            ],
            "taskAcknowledgements": [
                {"operationId": item["id"], "outcome": "applied", "reason": ""}
                for item in request["taskOperations"]
            ],
            "durationAcknowledgements": [
                {"operationId": item["id"], "outcome": "applied", "reason": ""}
                for item in request["durationOperations"]
            ],
            "autoStartAcknowledgements": [
                {"operationId": item["id"], "outcome": "applied", "reason": ""}
                for item in auto_start_operations
            ],
            "revision": revision,
            "canonicalTimer": None,
            "history": history or [],
            "tasks": tasks or [],
            "durationsMs": {
                "focus": 25 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
            "autoStartBreaks": auto_start_breaks,
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }

    def _queue_completed_timer(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": None,
        }
        self.store.queue_command(
            "finish", timer, "focus", settings["durationsMs"], now_ms=2_000
        )

    def test_bootstrap_resolution_strategy_preserves_one_sided_history(self) -> None:
        empty_remote = self._canonical_response(
            {"commands": [], "taskOperations": [], "durationOperations": []},
            revision=7,
        )
        self.assertEqual(
            self.store.bootstrap_resolution_plan(empty_remote)["strategy"],
            "keep_remote",
        )

        self.store.queue_task_operation("upsert", task_from_title("Local task"))
        self.assertEqual(
            self.store.bootstrap_resolution_plan(empty_remote)["strategy"], "merge"
        )
        self.store.reset_account_data()

        self._queue_completed_timer()
        local_only = self.store.bootstrap_resolution_plan(empty_remote)
        self.assertTrue(local_only["localHistory"])
        self.assertEqual(local_only["strategy"], "replace_remote")

        remote_response = deepcopy(empty_remote)
        remote_response["history"] = [self._history_item("remote")]
        both = self.store.bootstrap_resolution_plan(remote_response)
        self.assertTrue(both["localHistory"])
        self.assertTrue(both["remoteHistory"])
        self.assertIsNone(both["strategy"])

        self.store.reset_account_data()
        remote_only = self.store.bootstrap_resolution_plan(remote_response)
        self.assertFalse(remote_only["localHistory"])
        self.assertEqual(remote_only["strategy"], "keep_remote")

    def test_pending_resolution_retries_exact_request_after_restart(self) -> None:
        task = task_from_title("Durable task")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1_000)
        user = {"id": "user-1", "email": "one@example.com"}
        request = self.store.prepare_resolution(user, 3, "merge")
        self.assertEqual(
            set(request),
            {
                "requestId",
                "deviceId",
                "expectedRevision",
                "strategy",
                "commands",
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
            },
        )
        self.assertEqual(request["taskOperations"], [operation])
        self.store.close()

        self.store = Store(self.path)
        retry = self.store.prepare_resolution(user, 99, "keep_remote")
        self.assertEqual(retry, request)
        self.assertEqual(self.store.pending_resolution("user-1")["request"], request)

    def test_concurrent_connections_cannot_replace_another_owner_resolution(
        self,
    ) -> None:
        operation = self.store.queue_task_operation(
            "upsert", task_from_title("Owner-bound task"), now_ms=1
        )
        barrier = Barrier(3)
        results = Queue()

        def prepare(user_id: str) -> None:
            store = Store(self.path)
            try:
                barrier.wait()
                request = store.prepare_resolution({"id": user_id}, 4, "merge")
                results.put(("request", user_id, request))
            except ValueError as error:
                results.put(("error", user_id, str(error)))
            finally:
                store.close()

        threads = [
            Thread(target=prepare, args=(user_id,))
            for user_id in ("user-1", "user-2")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        outcomes = [results.get_nowait(), results.get_nowait()]
        requests = [outcome for outcome in outcomes if outcome[0] == "request"]
        errors = [outcome for outcome in outcomes if outcome[0] == "error"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("belongs to another account", errors[0][2])

        pending = self.store.pending_resolution()
        self.assertEqual(pending["owner"]["id"], requests[0][1])
        self.assertEqual(pending["request"], requests[0][2])
        self.assertEqual(pending["request"]["taskOperations"], [operation])
        self.assertEqual(self.store.load()["pendingTasks"], [operation])

    def test_pending_resolution_blocks_store_mutations_and_normal_sync(self) -> None:
        task = task_from_title("Claimed task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.prepare_resolution({"id": "user-1"}, 3, "merge")
        settings = self.store.load()["settings"]
        before = self.store.load()

        operations = (
            lambda: self.store.queue_command(
                "start", None, "focus", settings["durationsMs"], now_ms=2
            ),
            lambda: self.store.queue_task_operation(
                "upsert", task_from_title("Blocked task"), now_ms=2
            ),
            lambda: self.store.queue_duration_operation(
                "focus", 30 * 60_000, now_ms=2
            ),
            lambda: self.store.set_selected_phase("long_break"),
            lambda: self.store.set_auto_start_breaks(True),
            self.store.sync_payload,
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "pending account history"):
                    operation()
                self.assertEqual(self.store.load(), before)

    def test_discarded_conflict_request_stays_discarded_after_restart(self) -> None:
        task = task_from_title("Retry after conflict")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1_000)
        user = {"id": "user-1"}
        stale = self.store.prepare_resolution(user, 3, "merge")

        self.assertFalse(
            self.store.discard_pending_resolution(user["id"], "other-request")
        )
        self.assertEqual(
            self.store.pending_resolution(user["id"])["request"], stale
        )
        self.assertTrue(
            self.store.discard_pending_resolution(user["id"], stale["requestId"])
        )
        self.store.close()
        self.store = Store(self.path)

        self.assertIsNone(self.store.pending_resolution(user["id"]))
        self.assertEqual(self.store.load()["pendingTasks"], [operation])
        fresh = self.store.prepare_resolution(user, 4, "merge")
        self.assertNotEqual(fresh["requestId"], stale["requestId"])
        self.assertEqual(fresh["expectedRevision"], 4)
        self.assertEqual(fresh["taskOperations"], [operation])

    def test_stale_resolution_response_cannot_apply_to_new_request(self) -> None:
        task = task_from_title("Concurrent retry")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1_000)
        user = {"id": "user-1"}
        stale = self.store.prepare_resolution(user, 3, "merge")
        self.assertTrue(
            self.store.discard_pending_resolution(user["id"], stale["requestId"])
        )
        fresh = self.store.prepare_resolution(user, 4, "merge")
        response = self._canonical_response(stale, revision=5, tasks=[task])

        with self.assertRaisesRegex(ValueError, "stale request"):
            self.store.apply_resolution(
                response, user, request_id=stale["requestId"]
            )

        self.assertEqual(
            self.store.pending_resolution(user["id"])["request"], fresh
        )
        self.assertEqual(self.store.load()["pendingTasks"], [operation])

    def test_resolution_strategies_install_exact_canonical_state(self) -> None:
        user = {"id": "user-1", "email": "one@example.com"}
        local_task = task_from_title("Local task")
        remote_task = task_from_title("Remote task")
        cases = (
            ("replace_remote", ["local"], [local_task], [local_task]),
            ("keep_remote", ["remote"], [remote_task], [remote_task]),
            (
                "merge",
                ["remote", "local"],
                [remote_task, local_task],
                [local_task, remote_task],
            ),
        )
        for strategy, history_ids, tasks, known_tasks in cases:
            with self.subTest(strategy=strategy):
                self.store.reset_account_data()
                self.store.queue_task_operation("upsert", local_task, now_ms=1)
                self.store.queue_duration_operation("focus", 30 * 60_000, now_ms=2)
                self._queue_completed_timer()
                self.store.set_selected_phase("long_break")
                self.store.set_auto_start_breaks(True)
                request = self.store.prepare_resolution(user, 4, strategy)
                expected_counts = (
                    (0, 0, 0, 0)
                    if strategy == "keep_remote"
                    else (2, 1, 1, 1)
                )
                self.assertEqual(
                    (
                        len(request["commands"]),
                        len(request["taskOperations"]),
                        len(request["durationOperations"]),
                        len(request["autoStartOperations"]),
                    ),
                    expected_counts,
                )
                history = [self._history_item(item_id) for item_id in history_ids]
                response = self._canonical_response(
                    request, revision=5, history=history, tasks=tasks
                )
                response["durationsMs"] = {
                    "focus": 40 * 60_000,
                    "short_break": 6 * 60_000,
                    "long_break": 16 * 60_000,
                }

                self.store.apply_resolution(response, user)

                self.assertEqual(
                    self.store.load(),
                    {
                        "settings": {
                            "selectedPhase": "long_break",
                            "durations": {
                                "focus": 40,
                                "short_break": 6,
                                "long_break": 16,
                            },
                            "durationsMs": response["durationsMs"],
                            "autoStartBreaks": strategy != "keep_remote",
                            "selectedTaskId": None,
                        },
                        "snapshot": {
                            "revision": 5,
                            "canonicalTimer": None,
                            "history": history,
                            "tasks": tasks,
                            "knownTasks": known_tasks,
                            "autoStartBreaks": strategy != "keep_remote",
                            "user": user,
                        },
                        "pending": [],
                        "pendingTasks": [],
                        "pendingDurations": [],
                        "pendingAutoStarts": [],
                        "pendingResolution": None,
                    },
                )

    def test_resolution_deletes_only_captured_queue_ids_and_rebases_newer_work(
        self,
    ) -> None:
        user = {"id": "user-1"}
        for strategy in ("keep_remote", "replace_remote", "merge"):
            with self.subTest(strategy=strategy):
                self.store.reset_account_data()
                settings = self.store.load()["settings"]
                old_command = self.store.queue_command(
                    "start",
                    None,
                    "focus",
                    settings["durationsMs"],
                    now_ms=1_000,
                )
                old_task = self.store.queue_task_operation(
                    "upsert", task_from_title("Old task"), now_ms=1_001
                )
                old_duration = self.store.queue_duration_operation(
                    "focus", 30 * 60_000, now_ms=1_002
                )
                request = self.store.prepare_resolution(user, 4, strategy)
                pending = self.store.pending_resolution(user["id"])
                self.assertEqual(
                    pending["queueIds"],
                    {
                        "commands": [old_command["id"]],
                        "taskOperations": [old_task["id"]],
                        "durationOperations": [old_duration["id"]],
                        "autoStartOperations": [],
                    },
                )

                new_command = {
                    **old_command,
                    "id": f"command-new-{strategy}",
                    "deviceSequence": old_command["deviceSequence"] + 1,
                    "timerId": f"timer-new-{strategy}",
                    "occurredAt": "1970-01-01T00:00:02.000Z",
                    "hlcWallMs": 2_000,
                }
                new_task_value = task_from_title("New task")
                new_task = {
                    "id": f"task-operation-new-{strategy}",
                    "taskId": new_task_value["id"],
                    "type": "upsert",
                    "title": new_task_value["title"],
                    "occurredAt": "1970-01-01T00:00:02.001Z",
                    "hlcWallMs": 2_001,
                    "hlcCounter": 0,
                }
                new_duration = {
                    "id": f"duration-operation-new-{strategy}",
                    "phase": "focus",
                    "durationMs": 35 * 60_000,
                    "occurredAt": "1970-01-01T00:00:02.002Z",
                    "hlcWallMs": 2_002,
                    "hlcCounter": 0,
                }
                self.store.connection.execute(
                    "INSERT INTO pending_commands(id, device_sequence, payload) "
                    "VALUES (?, ?, ?)",
                    (
                        new_command["id"],
                        new_command["deviceSequence"],
                        json.dumps(new_command, separators=(",", ":")),
                    ),
                )
                self.store.connection.execute(
                    "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
                    (
                        new_task["id"],
                        json.dumps(new_task, separators=(",", ":")),
                    ),
                )
                self.store.connection.execute(
                    "INSERT INTO pending_duration_operations(id, phase, payload) "
                    "VALUES (?, ?, ?) ON CONFLICT(phase) DO UPDATE SET "
                    "id = excluded.id, payload = excluded.payload",
                    (
                        new_duration["id"],
                        new_duration["phase"],
                        json.dumps(new_duration, separators=(",", ":")),
                    ),
                )
                self.store._set_meta(
                    "deviceSequence", new_command["deviceSequence"]
                )
                self.store.connection.commit()
                response = self._canonical_response(request, revision=5)

                self.store.apply_resolution(response, user)

                loaded = self.store.load()
                self.assertEqual(loaded["pending"], [new_command])
                self.assertEqual(loaded["pendingTasks"], [new_task])
                self.assertEqual(loaded["pendingDurations"], [new_duration])
                timer, _history = rebuild_optimistic(
                    loaded["snapshot"]["canonicalTimer"],
                    loaded["snapshot"]["history"],
                    loaded["pending"],
                )
                self.assertEqual(timer["id"], new_command["timerId"])
                self.assertEqual(
                    rebuild_tasks(
                        loaded["snapshot"]["tasks"], loaded["pendingTasks"]
                    ),
                    [new_task_value],
                )
                self.assertEqual(loaded["settings"]["durations"]["focus"], 35)
                self.assertIsNone(loaded["pendingResolution"])

    def test_resolution_operation_limit_accepts_4096_and_rejects_4097(self) -> None:
        user = {"id": "user-1"}

        def insert_task_operations(count: int) -> None:
            rows = [
                (
                    f"operation-{index}",
                    json.dumps({"id": f"operation-{index}"}, separators=(",", ":")),
                )
                for index in range(count)
            ]
            self.store.connection.executemany(
                "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
                rows,
            )
            self.store.connection.commit()

        insert_task_operations(4_096)
        request = self.store.prepare_resolution(user, 1, "merge")
        self.assertEqual(len(request["taskOperations"]), 4_096)

        self.store.reset_account_data()
        insert_task_operations(4_097)
        with self.assertRaisesRegex(ValueError, "at most 4096 task operations"):
            self.store.prepare_resolution(user, 1, "merge")

        loaded = self.store.load()
        self.assertEqual(len(loaded["pendingTasks"]), 4_097)
        self.assertIsNone(loaded["pendingResolution"])
        self.store.connection.execute(
            "DELETE FROM pending_task_operations WHERE id = ?", ("operation-4096",)
        )
        self.store.connection.commit()
        recovered = self.store.prepare_resolution(user, 2, "merge")
        self.assertEqual(len(recovered["taskOperations"]), 4_096)
        self.assertEqual(recovered["expectedRevision"], 2)

    def test_resolution_malformed_response_rolls_back_all_local_data(self) -> None:
        self._queue_completed_timer()
        user = {"id": "user-1"}
        request = self.store.prepare_resolution(user, 2, "merge")
        before = self.store.load()
        malformed = self._canonical_response(request, revision=3)
        malformed["history"] = "not-a-list"

        with self.assertRaisesRegex(ValueError, "timer history"):
            self.store.apply_resolution(malformed, user)

        self.assertEqual(self.store.load(), before)

    def test_sync_requires_every_canonical_field_before_mutation(self) -> None:
        task = task_from_title("Strict response")
        task_operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=2
        )
        duration = self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=3
        )
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1, tasks=[task])
        before = self.store.load()

        required = (
            "acknowledgements",
            "taskAcknowledgements",
            "durationAcknowledgements",
            "autoStartAcknowledgements",
            "revision",
            "canonicalTimer",
            "history",
            "tasks",
            "durationsMs",
            "autoStartBreaks",
            "serverHlcWallMs",
            "serverHlcCounter",
        )
        for key in required:
            with self.subTest(key=key):
                malformed = deepcopy(response)
                malformed.pop(key)
                with self.assertRaisesRegex(ValueError, "canonical fields"):
                    self.store.apply_sync(malformed, request)
                self.assertEqual(self.store.load(), before)

        self.assertEqual(before["pending"], [command])
        self.assertEqual(before["pendingTasks"], [task_operation])
        self.assertEqual(before["pendingDurations"], [duration])

    def test_bootstrap_requires_complete_canonical_response(self) -> None:
        request = {"commands": [], "taskOperations": [], "durationOperations": []}
        response = self._canonical_response(request, revision=2)
        before = self.store.load()

        for key in ("canonicalTimer", "tasks", "taskAcknowledgements"):
            with self.subTest(key=key):
                malformed = deepcopy(response)
                malformed.pop(key)
                with self.assertRaisesRegex(ValueError, "canonical fields"):
                    self.store.bootstrap_resolution_plan(malformed)
                self.assertEqual(self.store.load(), before)

    def test_sync_rejects_malformed_canonical_items_atomically(self) -> None:
        task = task_from_title("Canonical task")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        base = self._canonical_response(request, revision=1, tasks=[task])
        valid_timer = {
            "id": "timer-server",
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 25 * 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": "2026-07-22T10:00:00.000Z",
            "taskId": task["id"],
            "lastIntent": {
                "type": "start",
                "commandId": "command-server",
                "occurredAt": "2026-07-22T10:00:00.000Z",
            },
        }
        before = self.store.load()
        malformed_values = (
            ("history", [{"id": "missing-shape"}]),
            ("history", [self._history_item("duplicate")] * 2),
            ("tasks", [{"id": task["id"], "title": "Different title"}]),
            ("tasks", [task, task]),
            ("canonicalTimer", {**valid_timer, "anchorAt": "not-a-date"}),
            (
                "canonicalTimer",
                {
                    **valid_timer,
                    "lastIntent": {"type": "unknown", "commandId": "command"},
                },
            ),
            (
                "durationsMs",
                {
                    "focus": 60_001,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
            ),
        )
        for key, value in malformed_values:
            with self.subTest(key=key, value=value):
                malformed = deepcopy(base)
                malformed[key] = value
                with self.assertRaises(ValueError):
                    self.store.apply_sync(malformed, request)
                self.assertEqual(self.store.load(), before)

    def test_task_sync_rejects_invalid_ack_outcome_and_reason_atomically(self) -> None:
        task = task_from_title("Strict task acknowledgement")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1, tasks=[task])
        before = self.store.load()

        for outcome, reason in (("unknown", ""), ("applied", None)):
            with self.subTest(outcome=outcome, reason=reason):
                malformed = deepcopy(response)
                malformed["taskAcknowledgements"] = [
                    {
                        "operationId": operation["id"],
                        "outcome": outcome,
                        "reason": reason,
                    }
                ]
                with self.assertRaisesRegex(ValueError, "task acknowledgements"):
                    self.store.apply_sync(malformed, request)
                self.assertEqual(self.store.load(), before)

    def test_resolution_cas_failure_preserves_request_and_revision(self) -> None:
        self._queue_completed_timer()
        user = {"id": "user-1"}
        request = self.store.prepare_resolution(user, 8, "replace_remote")
        stale = self._canonical_response(request, revision=7)

        with self.assertRaisesRegex(ValueError, "regress canonical revision"):
            self.store.apply_resolution(stale, user)

        loaded = self.store.load()
        self.assertEqual(loaded["snapshot"]["revision"], 0)
        self.assertEqual(loaded["pendingResolution"]["request"], request)
        self.assertEqual(len(loaded["pending"]), 2)

    def test_resolution_accepts_revision_equal_to_expected(self) -> None:
        self._queue_completed_timer()
        user = {"id": "user-1"}
        request = self.store.prepare_resolution(user, 8, "replace_remote")
        response = self._canonical_response(
            request,
            revision=8,
            history=[self._history_item("local")],
        )

        self.store.apply_resolution(response, user)

        loaded = self.store.load()
        self.assertEqual(loaded["snapshot"]["revision"], 8)
        self.assertEqual(loaded["snapshot"]["history"], response["history"])
        self.assertIsNone(loaded["pendingResolution"])

    def test_command_is_durable_before_reducer(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            now_ms=1_784_548_800_000,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [queued])
        timer, _history = rebuild_optimistic(None, [], loaded["pending"])
        self.assertEqual(timer["status"], "running")

    def test_sync_acknowledgement_removes_command(self) -> None:
        settings = self.store.load()["settings"]
        queued = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            now_ms=1_784_548_800_000,
        )
        request = self.store.sync_payload()
        self.store.apply_sync(
            {
                "acknowledgements": [
                    {"commandId": queued["id"], "outcome": "applied", "reason": ""}
                ],
                "taskAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": {
                    "id": queued["timerId"],
                    "phase": "focus",
                    "status": "running",
                    "plannedDurationMs": 1_500_000,
                    "elapsedAtAnchorMs": 0,
                    "anchorAt": queued["occurredAt"],
                },
                "history": [],
                "tasks": [],
                "durationAcknowledgements": [],
                "autoStartAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": False,
                "serverHlcWallMs": queued["hlcWallMs"],
                "serverHlcCounter": queued["hlcCounter"],
            },
            request,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["snapshot"]["revision"], 1)

    def test_task_operation_is_durable_and_reconciles(self) -> None:
        task = task_from_title("Write release notes")
        operation = self.store.queue_task_operation(
            "upsert", task, now_ms=1_784_548_800_000
        )
        payload = self.store.sync_payload()
        self.assertEqual(payload["taskOperations"], [operation])
        self.assertEqual(payload["commands"], [])

        self.store.apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [
                    {
                        "operationId": operation["id"],
                        "outcome": "applied",
                        "reason": "",
                    }
                ],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [task],
                "durationAcknowledgements": [],
                "autoStartAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": False,
                "serverHlcWallMs": operation["hlcWallMs"],
                "serverHlcCounter": operation["hlcCounter"],
            },
            payload,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

    def test_task_delete_ack_removes_task_but_retains_known_title(self) -> None:
        task = task_from_title("Delete after release")
        upsert = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(request, revision=1, tasks=[task]), request
        )
        self.assertEqual(request["taskOperations"], [upsert])

        deletion = self.store.queue_task_operation("delete", task, now_ms=2)
        request = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(request, revision=2, tasks=[]), request
        )

        loaded = self.store.load()
        self.assertEqual(request["taskOperations"], [deletion])
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

    def test_rejected_task_ack_is_terminal_and_reports_reason(self) -> None:
        task = task_from_title("Rejected task")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response["taskAcknowledgements"] = [
            {
                "operationId": operation["id"],
                "outcome": "rejected",
                "reason": "Task rejected by policy.",
            }
        ]

        notices = self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertEqual(notices, ["Task rejected by policy."])
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

    def test_task_sync_batches_rebases_and_applies_remote_deletion(self) -> None:
        operations = []
        tasks = []
        for index in range(257):
            task = task_from_title(f"Task {index:03d}")
            tasks.append(task)
            operations.append(
                self.store.queue_task_operation("upsert", task, now_ms=index + 1)
            )

        request = self.store.sync_payload()
        self.assertEqual(len(request["taskOperations"]), 256)
        response = self._canonical_response(
            request, revision=1, tasks=tasks[:256]
        )
        self.store.apply_sync(response, request)
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [operations[256]])

        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=2, tasks=tasks)
        self.store.apply_sync(response, request)
        self.assertEqual(self.store.load()["pendingTasks"], [])

        pull = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(pull, revision=3, tasks=[]), pull
        )
        loaded = self.store.load()
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(len(loaded["snapshot"]["knownTasks"]), 257)

    def test_task_sync_rebases_same_task_operation_queued_during_request(self) -> None:
        task = task_from_title("Same task")
        sent = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        replacement = self.store.queue_task_operation("delete", task, now_ms=2)

        self.store.apply_sync(
            self._canonical_response(request, revision=1, tasks=[task]), request
        )

        loaded = self.store.load()
        self.assertEqual(request["taskOperations"], [sent])
        self.assertEqual(loaded["pendingTasks"], [replacement])
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(
            rebuild_tasks(loaded["snapshot"]["tasks"], loaded["pendingTasks"]),
            [],
        )

    def test_duration_operation_compacts_and_persists_setting(self) -> None:
        first = self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=1_000
        )
        second = self.store.queue_duration_operation(
            "focus", 35 * 60_000, now_ms=1_000
        )

        loaded = self.store.load()
        self.assertEqual(loaded["pendingDurations"], [second])
        self.assertEqual(loaded["settings"]["durations"]["focus"], 35)
        self.assertEqual(loaded["settings"]["durationsMs"]["focus"], 35 * 60_000)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["hlcCounter"], first["hlcCounter"] + 1)
        self.assertEqual(
            set(second),
            {
                "id",
                "phase",
                "durationMs",
                "occurredAt",
                "hlcWallMs",
                "hlcCounter",
            },
        )
        self.assertEqual(self.store.sync_payload()["durationOperations"], [second])

    def test_concurrent_duration_edits_serialize_hlc_and_latest_value(self) -> None:
        ready = Event()
        start_edit = Event()
        attempting_edit = Event()
        results: list[dict] = []
        errors: list[BaseException] = []

        def edit_from_second_connection() -> None:
            store = Store(self.path)
            ready.set()
            start_edit.wait()
            attempting_edit.set()
            try:
                results.append(
                    store.queue_duration_operation(
                        "focus", 27 * 60_000, now_ms=1_000
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                store.close()

        thread = Thread(target=edit_from_second_connection)
        thread.start()
        self.assertTrue(ready.wait(2))

        self.store.connection.execute("BEGIN IMMEDIATE")
        try:
            settings = self.store._normalize_settings(
                self.store.get_meta("settings", {})
            )
            first = self.store._queue_duration_operation(
                "focus", 30 * 60_000, settings, now_ms=2_000
            )
            start_edit.set()
            self.assertTrue(attempting_edit.wait(2))
            self.store.connection.commit()
        except BaseException:
            self.store.connection.rollback()
            raise

        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        second = results[0]
        self.assertEqual(
            (second["hlcWallMs"], second["hlcCounter"]),
            (first["hlcWallMs"], first["hlcCounter"] + 1),
        )
        self.assertEqual(self.store.load()["pendingDurations"], [second])
        self.assertEqual(self.store.load()["settings"]["durations"]["focus"], 27)

    def test_stale_general_settings_save_preserves_synced_preferences(self) -> None:
        stale = self.store.load()["settings"]
        other = Store(self.path)
        try:
            other.queue_duration_operation("focus", 30 * 60_000, now_ms=1_000)
        finally:
            other.close()

        stale["autoStartBreaks"] = True
        self.store.save_settings(stale)

        settings = self.store.load()["settings"]
        self.assertEqual(settings["durationsMs"]["focus"], 30 * 60_000)
        self.assertFalse(settings["autoStartBreaks"])

    def test_sync_applies_canonical_then_replays_newer_in_flight_edit(self) -> None:
        sent = self.store.queue_duration_operation(
            "focus", 26 * 60_000, now_ms=1_000
        )
        request = self.store.sync_payload()
        replacement = self.store.queue_duration_operation(
            "focus", 27 * 60_000, now_ms=2_000
        )
        response = {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [
                {"operationId": sent["id"], "outcome": "applied", "reason": ""}
            ],
            "autoStartAcknowledgements": [],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 30 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
            "autoStartBreaks": False,
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }
        self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertEqual(
            loaded["settings"]["durations"],
            {"focus": 27, "short_break": 6, "long_break": 16},
        )
        self.assertEqual(loaded["pendingDurations"], [replacement])

        request = self.store.sync_payload()
        response["revision"] = 2
        response["durationAcknowledgements"] = [
            {
                "operationId": replacement["id"],
                "outcome": "applied",
                "reason": "",
            }
        ]
        response["durationsMs"]["focus"] = 27 * 60_000
        self.store.apply_sync(response, request)
        loaded = self.store.load()
        self.assertEqual(loaded["pendingDurations"], [])
        self.assertEqual(loaded["settings"]["durations"]["focus"], 27)

    def test_sync_write_lock_serializes_concurrent_duration_edit(self) -> None:
        server_wall = int(time.time() * 1000) + 60_000
        sent = self.store.queue_duration_operation(
            "focus", 26 * 60_000, now_ms=1_000
        )
        request = self.store.sync_payload()
        response = {
            "acknowledgements": [],
            "taskAcknowledgements": [],
            "durationAcknowledgements": [
                {"operationId": sent["id"], "outcome": "applied", "reason": ""}
            ],
            "autoStartAcknowledgements": [],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 30 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
            "autoStartBreaks": False,
            "serverHlcWallMs": server_wall,
            "serverHlcCounter": 0,
        }
        apply_ready = Event()
        start_apply = Event()
        canonical_entered = Event()
        release_apply = Event()
        edit_ready = Event()
        start_edit = Event()
        attempting_edit = Event()
        edits: list[dict] = []
        errors: list[BaseException] = []

        class BlockingStore(Store):
            def _canonical_durations(self, durations_ms: object) -> dict[str, int]:
                canonical_entered.set()
                if not release_apply.wait(2):
                    raise TimeoutError("test did not release sync transaction")
                return Store._canonical_durations(durations_ms)

        def apply_from_second_connection() -> None:
            store = BlockingStore(self.path)
            apply_ready.set()
            start_apply.wait()
            try:
                store.apply_sync(response, request)
            except BaseException as error:
                errors.append(error)
            finally:
                store.close()

        def edit_from_third_connection() -> None:
            store = Store(self.path)
            edit_ready.set()
            start_edit.wait()
            attempting_edit.set()
            try:
                edits.append(
                    store.queue_duration_operation(
                        "focus", 27 * 60_000, now_ms=2_000
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                store.close()

        apply_thread = Thread(target=apply_from_second_connection)
        edit_thread = Thread(target=edit_from_third_connection)
        apply_thread.start()
        edit_thread.start()
        try:
            self.assertTrue(apply_ready.wait(2))
            self.assertTrue(edit_ready.wait(2))
            start_apply.set()
            self.assertTrue(canonical_entered.wait(2))
            start_edit.set()
            self.assertTrue(attempting_edit.wait(2))
        finally:
            start_apply.set()
            start_edit.set()
            release_apply.set()
            apply_thread.join(2)
            edit_thread.join(2)

        self.assertFalse(apply_thread.is_alive())
        self.assertFalse(edit_thread.is_alive())
        self.assertEqual(errors, [])
        edit = edits[0]
        self.assertEqual(
            (edit["hlcWallMs"], edit["hlcCounter"]), (server_wall, 1)
        )
        self.assertEqual(self.store.load()["pendingDurations"], [edit])
        self.assertEqual(self.store.load()["settings"]["durations"]["focus"], 27)

    def test_sync_rejects_non_exact_acknowledgements_atomically(self) -> None:
        task = task_from_title("Review sync")
        task_operation = self.store.queue_task_operation(
            "upsert", task, now_ms=900
        )
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=950
        )
        duration_operation = self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=1_000
        )
        auto_start_operation = self.store.set_auto_start_breaks(True, now_ms=1_001)
        request = self.store.sync_payload()
        response: dict[str, object] = {
            "acknowledgements": [
                {
                    "commandId": command["id"],
                    "outcome": "applied",
                    "reason": "",
                }
            ],
            "taskAcknowledgements": [
                {
                    "operationId": task_operation["id"],
                    "outcome": "applied",
                    "reason": "",
                }
            ],
            "durationAcknowledgements": [
                {
                    "operationId": duration_operation["id"],
                    "outcome": "applied",
                    "reason": "",
                }
            ],
            "autoStartAcknowledgements": [
                {
                    "operationId": auto_start_operation["id"],
                    "outcome": "applied",
                    "reason": "",
                }
            ],
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 40 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
            "autoStartBreaks": False,
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }

        invalid_sets = (
            ("acknowledgements", "command"),
            ("taskAcknowledgements", "task"),
            ("durationAcknowledgements", "duration"),
            ("autoStartAcknowledgements", "auto-start"),
        )
        for key, label in invalid_sets:
            for invalid in ([], [*response[key], *response[key]]):
                with self.subTest(key=key, acknowledgements=invalid):
                    invalid_response = deepcopy(response)
                    invalid_response[key] = invalid
                    with self.assertRaisesRegex(
                        ValueError, f"{label} acknowledgement set"
                    ):
                        self.store.apply_sync(invalid_response, request)

        invalid_canonical = deepcopy(response)
        invalid_canonical["durationsMs"]["focus"] = 60_001
        with self.assertRaisesRegex(ValueError, "whole minutes"):
            self.store.apply_sync(invalid_canonical, request)

        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [command])
        self.assertEqual(loaded["pendingTasks"], [task_operation])
        self.assertEqual(loaded["pendingDurations"], [duration_operation])
        self.assertEqual(loaded["pendingAutoStarts"], [auto_start_operation])
        self.assertEqual(loaded["settings"]["durations"]["focus"], 30)
        self.assertEqual(loaded["snapshot"]["revision"], 0)

    def test_remote_duration_pull_preserves_local_controls(self) -> None:
        self.store.set_selected_phase("long_break")
        self.store.set_auto_start_breaks(True)
        request = self.store.sync_payload()
        self.assertEqual(
            set(request),
            {
                "deviceId",
                "lastRevision",
                "commands",
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
            },
        )
        server_hlc_wall_ms = int(time.time() * 1000) + 60_000
        self.store.apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [],
                "autoStartAcknowledgements": [
                    {
                        "operationId": item["id"],
                        "outcome": "applied",
                        "reason": "",
                    }
                    for item in request["autoStartOperations"]
                ],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": {
                    "focus": 2 * 60_000,
                    "short_break": 60_000,
                    "long_break": 10_800_000,
                },
                "autoStartBreaks": True,
                "serverHlcWallMs": server_hlc_wall_ms,
                "serverHlcCounter": 7,
            },
            request,
        )

        settings = self.store.load()["settings"]
        self.assertEqual(
            self.store.get_meta("hlc"),
            {"wallMs": server_hlc_wall_ms, "counter": 7},
        )
        self.assertEqual(
            settings["durationsMs"],
            {
                "focus": 2 * 60_000,
                "short_break": 60_000,
                "long_break": 10_800_000,
            },
        )
        self.assertEqual(
            settings["durations"],
            {"focus": 2, "short_break": 1, "long_break": 180},
        )
        self.assertEqual(settings["selectedPhase"], "long_break")
        self.assertTrue(settings["autoStartBreaks"])
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.assertEqual(command["plannedDurationMs"], 2 * 60_000)
        self.assertEqual(
            (command["hlcWallMs"], command["hlcCounter"]),
            (server_hlc_wall_ms, 8),
        )

    def test_duration_bounds_are_enforced(self) -> None:
        for duration_ms in (59_999, 60_001, 10_800_001):
            with self.subTest(duration_ms=duration_ms):
                with self.assertRaises(ValueError):
                    self.store.queue_duration_operation("focus", duration_ms)

                request = self.store.sync_payload()
                with self.assertRaises(ValueError):
                    self.store.apply_sync(
                        {
                            "acknowledgements": [],
                            "taskAcknowledgements": [],
                            "durationAcknowledgements": [],
                            "autoStartAcknowledgements": [],
                            "revision": 1,
                            "canonicalTimer": None,
                            "history": [],
                            "tasks": [],
                            "durationsMs": {
                                "focus": duration_ms,
                                "short_break": 5 * 60_000,
                                "long_break": 15 * 60_000,
                            },
                            "autoStartBreaks": False,
                            "serverHlcWallMs": 1_000,
                            "serverHlcCounter": 0,
                        },
                        request,
                    )

        self.assertEqual(self.store.load()["snapshot"]["revision"], 0)

    def test_canonical_timer_durations_allow_four_hours(self) -> None:
        request = self.store.sync_payload()
        canonical_timer = {
            "id": "timer-four-hours",
            "phase": "focus",
            "status": "paused",
            "plannedDurationMs": 14_400_000,
            "elapsedAtAnchorMs": 10_800_000,
            "anchorAt": "2026-07-22T10:00:00.000Z",
        }
        history = self._history_item("four-hours")
        history["plannedDurationMs"] = 14_400_000
        response = self._canonical_response(request, history=[history])
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        loaded = self.store.load()["snapshot"]
        self.assertEqual(loaded["canonicalTimer"], canonical_timer)
        self.assertEqual(loaded["history"], [history])

        invalid = deepcopy(response)
        invalid["revision"] = 2
        invalid["canonicalTimer"]["plannedDurationMs"] = 14_460_000
        with self.assertRaisesRegex(ValueError, "canonical timer"):
            self.store.apply_sync(invalid, self.store.sync_payload())

    def test_legacy_duration_migration_queues_only_custom_values_once(self) -> None:
        self.store.set_meta(
            "settings",
            {
                "selectedPhase": "unknown",
                "durations": {"focus": "30", "short_break": 0},
                "autoStartBreaks": 0,
            },
        )
        self.store.set_meta("durationMigrationComplete", False)
        self.store.close()

        self.store = Store(self.path)
        loaded = self.store.load()
        self.assertEqual(
            loaded["settings"]["durations"],
            {"focus": 30, "short_break": 5, "long_break": 15},
        )
        self.assertEqual(
            loaded["settings"]["durationsMs"],
            {
                "focus": 30 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
        )
        self.assertEqual(loaded["settings"]["selectedPhase"], "focus")
        self.assertEqual(len(loaded["pendingDurations"]), 1)
        bootstrap = loaded["pendingDurations"][0]
        self.assertEqual(bootstrap["phase"], "focus")
        self.assertEqual((bootstrap["hlcWallMs"], bootstrap["hlcCounter"]), (0, 0))
        operation_id = bootstrap["id"]

        real_edit = self.store.queue_duration_operation(
            "short_break", 6 * 60_000, now_ms=1
        )
        self.assertGreater(
            (real_edit["hlcWallMs"], real_edit["hlcCounter"]), (0, 0)
        )

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(
            self.store.load()["pendingDurations"][0]["id"], operation_id
        )

    def test_v013_keep_remote_resolution_discards_auto_start_migration(self) -> None:
        self.store.close()
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pending_commands (
                id TEXT PRIMARY KEY,
                device_sequence INTEGER NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            CREATE TABLE pending_task_operations (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE pending_duration_operations (
                id TEXT PRIMARY KEY,
                phase TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            """
        )
        user = {"id": "user-1"}
        resolution_request = {
            "requestId": "legacy-resolution",
            "deviceId": "desktop-v013",
            "expectedRevision": 0,
            "strategy": "keep_remote",
            "commands": [],
            "taskOperations": [],
            "durationOperations": [],
        }
        legacy = {
            "deviceId": "desktop-v013",
            "deviceSequence": 0,
            "hlc": {"wallMs": 0, "counter": 0},
            "settings": {
                "selectedPhase": "focus",
                "durations": {"focus": 25, "short_break": 5, "long_break": 15},
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": True,
                "selectedTaskId": None,
            },
            "snapshot": {
                "revision": 0,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "knownTasks": [],
                "user": None,
            },
            "pendingResolution": {
                "owner": user,
                "request": resolution_request,
                "queueIds": {
                    "commands": [],
                    "taskOperations": [],
                    "durationOperations": [],
                },
            },
            "durationMigrationComplete": True,
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (
                (key, json.dumps(value, separators=(",", ":")))
                for key, value in legacy.items()
            ),
        )
        connection.commit()
        connection.close()

        self.store = Store(self.path)
        loaded = self.store.load()

        self.assertTrue(loaded["settings"]["autoStartBreaks"])
        self.assertFalse(loaded["snapshot"]["autoStartBreaks"])
        self.assertEqual(len(loaded["pendingAutoStarts"]), 1)
        self.assertTrue(loaded["pendingAutoStarts"][0]["enabled"])
        self.assertFalse(self.store.has_pending_auto_break())

        self.store.apply_resolution(
            self._canonical_response(
                resolution_request, revision=1, auto_start_breaks=False
            ),
            user,
        )
        resolved = self.store.load()
        self.assertEqual(resolved["pendingAutoStarts"], [])
        self.assertFalse(resolved["settings"]["autoStartBreaks"])
        self.assertFalse(resolved["snapshot"]["autoStartBreaks"])
        self.assertIsNone(self.store.pending_resolution())

    def test_legacy_auto_start_migration_queues_true_once_and_persists(self) -> None:
        settings = self.store.load()["settings"]
        settings["autoStartBreaks"] = True
        self.store.set_meta("settings", settings)
        self.store.set_meta("autoStartMigrationComplete", False)
        self.store.close()

        self.store = Store(self.path)
        loaded = self.store.load()
        self.assertTrue(loaded["settings"]["autoStartBreaks"])
        self.assertEqual(len(loaded["pendingAutoStarts"]), 1)
        operation = loaded["pendingAutoStarts"][0]
        self.assertEqual(
            set(operation),
            {
                "id",
                "deviceId",
                "enabled",
                "occurredAt",
                "hlcWallMs",
                "hlcCounter",
            },
        )
        self.assertEqual(operation["deviceId"], self.store.device_id)
        self.assertTrue(operation["enabled"])
        self.assertEqual(
            (operation["occurredAt"], operation["hlcWallMs"], operation["hlcCounter"]),
            ("1970-01-01T00:00:00.000Z", 0, 0),
        )

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.load()["pendingAutoStarts"], [operation])

    def test_legacy_untouched_false_preserves_remote_true_on_replacement(
        self,
    ) -> None:
        settings = self.store.load()["settings"]
        settings["autoStartBreaks"] = False
        snapshot = self.store.load()["snapshot"]
        snapshot.pop("autoStartBreaks")
        self.store.set_meta("settings", settings)
        self.store.set_meta("snapshot", snapshot)
        self.store.set_meta("autoStartMigrationComplete", False)
        self.store.close()

        self.store = Store(self.path)
        self.assertTrue(self.store.get_meta("autoStartLegacyDefaultUnknown"))
        request = self.store.prepare_resolution(
            {"id": "user-1"}, 1, "replace_remote"
        )
        self.assertNotIn("autoStartOperations", request)
        self.store.apply_resolution(
            self._canonical_response(
                request, revision=1, auto_start_breaks=True
            ),
            {"id": "user-1"},
        )
        self.assertTrue(self.store.load()["settings"]["autoStartBreaks"])
        self.assertFalse(self.store.get_meta("autoStartLegacyDefaultUnknown"))

        explicit_false = self.store.set_auto_start_breaks(False, now_ms=2_000)
        explicit = self.store.prepare_resolution(
            {"id": "user-1"}, 2, "replace_remote"
        )
        self.assertEqual(explicit["autoStartOperations"], [explicit_false])

    def test_auto_start_toggles_are_immutable_durable_operations(self) -> None:
        enabled = self.store.set_auto_start_breaks(True, now_ms=1_000)
        disabled = self.store.set_auto_start_breaks(False, now_ms=1_000)

        loaded = self.store.load()
        self.assertEqual(loaded["pendingAutoStarts"], [enabled, disabled])
        self.assertFalse(loaded["settings"]["autoStartBreaks"])
        self.assertEqual(
            disabled["hlcCounter"], enabled["hlcCounter"] + 1
        )

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(
            self.store.load()["pendingAutoStarts"], [enabled, disabled]
        )

    def test_auto_start_true_false_sync_and_remote_pull(self) -> None:
        for revision, enabled in ((1, True), (2, False)):
            with self.subTest(enabled=enabled):
                operation = self.store.set_auto_start_breaks(
                    enabled, now_ms=revision * 1_000
                )
                request = self.store.sync_payload()
                response = self._canonical_response(
                    request,
                    revision=revision,
                    auto_start_breaks=enabled,
                )
                self.store.apply_sync(response, request)
                loaded = self.store.load()
                self.assertEqual(
                    response["autoStartAcknowledgements"][0]["operationId"],
                    operation["id"],
                )
                self.assertEqual(loaded["pendingAutoStarts"], [])
                self.assertEqual(loaded["settings"]["autoStartBreaks"], enabled)
                self.assertEqual(
                    loaded["snapshot"]["autoStartBreaks"], enabled
                )

        pull = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(
                pull, revision=3, auto_start_breaks=True
            ),
            pull,
        )
        self.assertTrue(self.store.load()["settings"]["autoStartBreaks"])

    def test_auto_start_sync_batches_257_operations_and_rebases(self) -> None:
        operations = [
            self.store.set_auto_start_breaks(index % 2 == 0, now_ms=1_000)
            for index in range(257)
        ]

        first = self.store.sync_payload()
        self.assertEqual(first["autoStartOperations"], operations[:256])
        self.store.apply_sync(
            self._canonical_response(
                first, revision=1, auto_start_breaks=operations[255]["enabled"]
            ),
            first,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingAutoStarts"], [operations[256]])
        self.assertEqual(
            loaded["settings"]["autoStartBreaks"], operations[256]["enabled"]
        )

        second = self.store.sync_payload()
        self.assertEqual(second["autoStartOperations"], [operations[256]])
        self.store.apply_sync(
            self._canonical_response(
                second, revision=2, auto_start_breaks=operations[256]["enabled"]
            ),
            second,
        )
        self.assertEqual(self.store.load()["pendingAutoStarts"], [])

    def test_auto_start_projection_has_deterministic_lww_ties(self) -> None:
        operations = [
            {
                "id": "operation-z",
                "deviceId": "device-a",
                "enabled": False,
                "hlcWallMs": 10,
                "hlcCounter": 2,
            },
            {
                "id": "operation-a",
                "deviceId": "device-b",
                "enabled": True,
                "hlcWallMs": 10,
                "hlcCounter": 2,
            },
            {
                "id": "operation-b",
                "deviceId": "device-b",
                "enabled": False,
                "hlcWallMs": 10,
                "hlcCounter": 2,
            },
        ]
        self.assertFalse(project_auto_start_breaks(True, operations))
        self.assertTrue(project_auto_start_breaks(False, operations[:2]))

    def test_auto_start_sync_rebases_toggle_created_in_flight(self) -> None:
        sent = self.store.set_auto_start_breaks(True, now_ms=1_000)
        request = self.store.sync_payload()
        replacement = self.store.set_auto_start_breaks(False, now_ms=2_000)

        self.store.apply_sync(
            self._canonical_response(
                request, revision=1, auto_start_breaks=True
            ),
            request,
        )

        loaded = self.store.load()
        self.assertEqual(request["autoStartOperations"], [sent])
        self.assertEqual(loaded["pendingAutoStarts"], [replacement])
        self.assertFalse(loaded["settings"]["autoStartBreaks"])
        self.assertTrue(loaded["snapshot"]["autoStartBreaks"])

    def test_bootstrap_auto_start_presence_legacy_omission_and_exact_retry(
        self,
    ) -> None:
        user = {"id": "user-1"}
        request = self.store.prepare_resolution(user, 3, "replace_remote")
        self.assertIn("autoStartOperations", request)
        self.assertEqual(request["autoStartOperations"], [])

        pending = self.store.pending_resolution(user["id"])
        legacy_request = dict(pending["request"])
        legacy_request.pop("autoStartOperations")
        legacy_queue_ids = dict(pending["queueIds"])
        legacy_queue_ids.pop("autoStartOperations")
        self.store.set_meta(
            "pendingResolution",
            {
                "owner": user,
                "request": legacy_request,
                "queueIds": legacy_queue_ids,
            },
        )
        self.store.close()

        self.store = Store(self.path)
        retry = self.store.prepare_resolution(user, 99, "keep_remote")
        self.assertEqual(retry, legacy_request)
        self.assertNotIn("autoStartOperations", retry)
        response = self._canonical_response(
            retry, revision=4, auto_start_breaks=True
        )
        self.store.apply_resolution(response, user)
        self.assertTrue(self.store.load()["settings"]["autoStartBreaks"])

        self.store.reset_account_data()
        explicit = self.store.prepare_resolution(user, 5, "replace_remote")
        self.assertEqual(explicit["autoStartOperations"], [])
        self.store.apply_resolution(
            self._canonical_response(
                explicit, revision=5, auto_start_breaks=False
            ),
            user,
        )
        self.assertFalse(self.store.load()["settings"]["autoStartBreaks"])

    def test_auto_start_resolution_limit_accepts_4096_and_rejects_4097(self) -> None:
        rows = []
        for index in range(4_097):
            operation = {
                "id": f"auto-operation-{index}",
                "deviceId": self.store.device_id,
                "enabled": bool(index % 2),
                "occurredAt": "1970-01-01T00:00:00.000Z",
                "hlcWallMs": index,
                "hlcCounter": 0,
            }
            rows.append(
                (operation["id"], json.dumps(operation, separators=(",", ":")))
            )
        self.store.connection.executemany(
            "INSERT INTO pending_auto_start_operations(id, payload) VALUES (?, ?)",
            rows[:4_096],
        )
        self.store.connection.commit()
        request = self.store.prepare_resolution({"id": "user-1"}, 1, "merge")
        self.assertEqual(len(request["autoStartOperations"]), 4_096)

        self.store.clear_pending_resolution()
        self.store.connection.execute(
            "INSERT INTO pending_auto_start_operations(id, payload) VALUES (?, ?)",
            rows[4_096],
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "at most 4096 auto-start operations"):
            self.store.prepare_resolution({"id": "user-1"}, 2, "merge")

    def test_concurrent_auto_start_toggles_serialize_hlc_and_projection(self) -> None:
        barrier = Barrier(3)
        results = Queue()

        def toggle(enabled: bool) -> None:
            store = Store(self.path)
            try:
                barrier.wait()
                results.put(store.set_auto_start_breaks(enabled, now_ms=1_000))
            finally:
                store.close()

        threads = [Thread(target=toggle, args=(enabled,)) for enabled in (True, False)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        operations = sorted(
            [results.get_nowait(), results.get_nowait()],
            key=lambda item: (item["hlcWallMs"], item["hlcCounter"]),
        )
        self.assertEqual(operations[0]["hlcWallMs"], operations[1]["hlcWallMs"])
        self.assertEqual(
            operations[1]["hlcCounter"], operations[0]["hlcCounter"] + 1
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingAutoStarts"], operations)
        self.assertEqual(
            loaded["settings"]["autoStartBreaks"], operations[-1]["enabled"]
        )

    def test_auto_start_off_and_remote_completion_do_not_start_break(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        timer = {
            "id": start["timerId"],
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": start["plannedDurationMs"],
            "elapsedAtAnchorMs": 0,
            "anchorAt": start["occurredAt"],
            "taskId": None,
        }
        self.store.queue_command(
            "finish", timer, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.assertFalse(self.store.has_pending_auto_break())
        self.store.set_auto_start_breaks(True, now_ms=2_500)
        before = len(self.store.load()["pending"])
        self.assertEqual(
            self.store.process_auto_break(require_canonical=False, now_ms=3_000), []
        )
        self.assertEqual(len(self.store.load()["pending"]), before)
        self.assertFalse(self.store.has_pending_auto_break())

        self.store.reset_account_data()
        operation = self.store.set_auto_start_breaks(True, now_ms=4_000)
        request = self.store.sync_payload()
        remote = self._history_item("remote-focus")
        response = self._canonical_response(
            request,
            revision=1,
            history=[remote],
            auto_start_breaks=True,
        )
        self.store.apply_sync(response, request)
        self.assertEqual(
            response["autoStartAcknowledgements"][0]["operationId"], operation["id"]
        )
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(
            self.store.process_auto_break(require_canonical=True, now_ms=5_000), []
        )
        self.assertEqual(self.store.load()["pending"], [])

    def test_converged_focus_count_starts_one_long_break_after_local_completion(
        self,
    ) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.assertEqual(
            self.store.process_auto_break(require_canonical=True, now_ms=3_000), []
        )

        request = self.store.sync_payload()
        completed, local_history = rebuild_optimistic(None, [], request["commands"])
        canonical_local = dict(local_history[0])
        canonical_local.pop("pending", None)
        remote_history = [self._history_item(f"remote-{index}") for index in range(3)]
        response = self._canonical_response(
            request,
            revision=1,
            history=[canonical_local, *remote_history],
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = completed
        self.store.apply_sync(response, request)

        commands = self.store.process_auto_break(
            require_canonical=True, now_ms=3_000
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["phase"], "long_break")
        self.assertEqual(commands[0]["type"], "start")
        self.assertEqual(finish["id"], canonical_local["commandId"])
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(
            self.store.process_auto_break(require_canonical=True, now_ms=4_000), []
        )
        self.assertEqual(
            [command["id"] for command in self.store.load()["pending"]],
            [commands[0]["id"]],
        )

    def test_duplicate_finish_queued_in_flight_keeps_auto_break_trigger(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        first_finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        request = self.store.sync_payload()
        duplicate_finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_001
        )
        canonical_timer, canonical_history = rebuild_optimistic(
            None, [], request["commands"]
        )
        for item in canonical_history:
            item.pop("pending", None)
        response = self._canonical_response(
            request,
            revision=1,
            history=canonical_history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer
        self.store.apply_sync(response, request)

        started = self.store.process_auto_break(
            require_canonical=True, now_ms=3_000
        )

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["phase"], "short_break")
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(first_finish["id"], canonical_history[0]["commandId"])
        self.assertEqual(
            [command["id"] for command in self.store.load()["pending"]],
            [duplicate_finish["id"], started[0]["id"]],
        )

    def test_stale_auto_break_trigger_does_not_block_newer_valid_trigger(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        first_running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish",
            first_running,
            "focus",
            settings["durationsMs"],
            now_ms=2_000,
        )
        first_completed, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "clear",
            first_completed,
            "focus",
            settings["durationsMs"],
            now_ms=2_001,
        )
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=3_000
        )
        second_running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish",
            second_running,
            "focus",
            settings["durationsMs"],
            now_ms=4_000,
        )
        trigger_count = self.store.connection.execute(
            "SELECT COUNT(*) FROM pending_auto_breaks"
        ).fetchone()[0]
        self.assertEqual(trigger_count, 2)

        started = self.store.process_auto_break(
            require_canonical=False, now_ms=5_000
        )

        self.assertEqual(len(started), 1)
        self.assertEqual((started[0]["type"], started[0]["phase"]), ("start", "short_break"))
        self.assertFalse(self.store.has_pending_auto_break())

    def test_auto_break_trigger_survives_restart_without_duplicate_start(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        before = len(self.store.load()["pending"])
        self.store.close()

        self.store = Store(self.path)
        self.assertTrue(self.store.has_pending_auto_break())
        started = self.store.process_auto_break(
            require_canonical=False, now_ms=3_000
        )
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.store.load()["pending"]), before + 1)
        self.store.close()

        self.store = Store(self.path)
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(
            self.store.process_auto_break(require_canonical=False, now_ms=4_000), []
        )
        self.assertEqual(len(self.store.load()["pending"]), before + 1)

    def test_crash_after_ack_discards_stale_auto_break_trigger_offline(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        completed, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "clear", completed, "focus", settings["durationsMs"], now_ms=2_001
        )
        newer_start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=2_002
        )
        request = self.store.sync_payload()
        canonical_timer, canonical_history = rebuild_optimistic(
            None, [], request["commands"]
        )
        for item in canonical_history:
            item.pop("pending", None)
        response = self._canonical_response(
            request,
            history=canonical_history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer
        self.store.apply_sync(response, request)
        self.store.close()
        self.store = Store(self.path)

        self.assertEqual(
            self.store.process_auto_break(require_canonical=False, now_ms=3_000), []
        )
        loaded = self.store.load()
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(
            loaded["snapshot"]["canonicalTimer"]["id"], newer_start["timerId"]
        )

    def test_remote_finish_cannot_consume_rejected_local_trigger_offline(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        request = self.store.sync_payload()
        completed, history = rebuild_optimistic(None, [], request["commands"])
        remote_command_id = "remote-finish"
        completed["lastIntent"]["commandId"] = remote_command_id
        history[0]["id"] = f"{start['timerId']}:{remote_command_id}"
        history[0]["commandId"] = remote_command_id
        history[0].pop("pending", None)
        response = self._canonical_response(
            request,
            revision=1,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = completed
        response["acknowledgements"] = [
            {"commandId": start["id"], "outcome": "applied", "reason": ""},
            {
                "commandId": finish["id"],
                "outcome": "rejected",
                "reason": "superseded by remote finish",
            },
        ]
        self.store.apply_sync(response, request)

        self.assertEqual(
            self.store.process_auto_break(require_canonical=False, now_ms=3_000), []
        )
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(self.store.load()["pending"], [])

    def test_remote_start_discards_stale_local_auto_break_trigger(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        request = self.store.sync_payload()
        _completed, canonical_history = rebuild_optimistic(
            None, [], request["commands"]
        )
        for item in canonical_history:
            item.pop("pending", None)
        remote_timer = {
            "id": "remote-break",
            "phase": "short_break",
            "status": "running",
            "plannedDurationMs": 5 * 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": "1970-01-01T00:00:03.000Z",
            "taskId": None,
        }
        response = self._canonical_response(
            request,
            history=canonical_history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = remote_timer
        self.store.apply_sync(response, request)

        self.assertEqual(
            self.store.process_auto_break(require_canonical=True, now_ms=4_000), []
        )
        loaded = self.store.load()
        self.assertFalse(self.store.has_pending_auto_break())
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["snapshot"]["canonicalTimer"], remote_timer)

    def test_concurrent_instances_cannot_duplicate_auto_break_start(self) -> None:
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        barrier = Barrier(3)
        results = Queue()

        def process() -> None:
            store = Store(self.path)
            try:
                barrier.wait()
                results.put(
                    store.process_auto_break(
                        require_canonical=False, now_ms=3_000
                    )
                )
            finally:
                store.close()

        threads = [Thread(target=process) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        outcomes = [results.get_nowait(), results.get_nowait()]
        self.assertEqual(sorted(len(outcome) for outcome in outcomes), [0, 1])
        starts = [
            command
            for command in self.store.load()["pending"]
            if command["type"] == "start" and command["phase"] == "short_break"
        ]
        self.assertEqual(len(starts), 1)

    def test_full_auto_start_cycle_converges_timer_history_tasks_and_durations(
        self,
    ) -> None:
        from pomodorough.terminal import LocalTimer

        task = task_from_title("Cycle task")
        self.store.queue_task_operation("upsert", task, now_ms=100)
        self.store.set_selected_task_id(task["id"])
        custom_durations = {
            "focus": 2 * 60_000,
            "short_break": 3 * 60_000,
            "long_break": 4 * 60_000,
        }
        for index, (phase, duration_ms) in enumerate(custom_durations.items()):
            self.store.queue_duration_operation(phase, duration_ms, now_ms=200 + index)
        self.store.set_auto_start_breaks(True, now_ms=300)
        timer = LocalTimer(self.store)

        now_ms = 1_000
        timer.issue("start", phase="focus", now_ms=now_ms)
        for focus_number in range(1, 5):
            now_ms += custom_durations["focus"]
            timer.issue("finish", now_ms=now_ms)
            state = timer.state(now_ms=now_ms)
            expected_break = "long_break" if focus_number == 4 else "short_break"
            self.assertEqual((state["phase"], state["status"]), (expected_break, "running"))
            self.assertEqual(state["plannedDurationMs"], custom_durations[expected_break])
            self.assertIsNone(state["taskId"])
            if focus_number == 4:
                break
            now_ms += custom_durations["short_break"]
            timer.issue("finish", now_ms=now_ms)
            timer.issue("clear", now_ms=now_ms + 1)
            timer.issue("start", phase="focus", now_ms=now_ms + 2)
            focus_state = timer.state(now_ms=now_ms + 2)
            self.assertEqual(focus_state["taskId"], task["id"])
            self.assertEqual(
                focus_state["plannedDurationMs"], custom_durations["focus"]
            )

        completed = timer.completed_history()
        self.assertEqual(
            [item["phase"] for item in completed],
            [
                "focus",
                "short_break",
                "focus",
                "short_break",
                "focus",
                "short_break",
                "focus",
            ][::-1],
        )
        focus_history = [item for item in completed if item["phase"] == "focus"]
        self.assertEqual(len(focus_history), 4)
        self.assertTrue(all(item["taskId"] == task["id"] for item in focus_history))
        self.assertEqual(self.store.load()["settings"]["selectedTaskId"], task["id"])

        request = self.store.sync_payload()
        canonical_history = []
        for item in completed:
            canonical_item = dict(item)
            canonical_item.pop("pending", None)
            canonical_item.pop("taskTitle", None)
            canonical_history.append(canonical_item)
        response = self._canonical_response(
            request,
            revision=1,
            history=canonical_history,
            tasks=[task],
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = deepcopy(timer.timer)
        response["durationsMs"] = custom_durations
        response["serverHlcWallMs"] = max(
            operation["hlcWallMs"]
            for key in (
                "commands",
                "taskOperations",
                "durationOperations",
                "autoStartOperations",
            )
            for operation in request[key]
        )
        self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["pendingDurations"], [])
        self.assertEqual(loaded["pendingAutoStarts"], [])
        self.assertEqual(loaded["snapshot"]["history"], canonical_history)
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(loaded["settings"]["durationsMs"], custom_durations)
        self.assertTrue(loaded["settings"]["autoStartBreaks"])

    def test_auto_start_lost_and_malformed_responses_preserve_exact_operation(
        self,
    ) -> None:
        operation = self.store.set_auto_start_breaks(True, now_ms=1_000)
        request = self.store.sync_payload()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.sync_payload()["autoStartOperations"], [operation])

        malformed = self._canonical_response(
            request, revision=1, auto_start_breaks=True
        )
        malformed["autoStartAcknowledgements"] = []
        before = self.store.load()
        with self.assertRaisesRegex(ValueError, "auto-start acknowledgement set"):
            self.store.apply_sync(malformed, request)
        self.assertEqual(self.store.load(), before)

    def test_focus_start_has_task_but_break_start_does_not(self) -> None:
        settings = self.store.load()["settings"]
        task = task_from_title("Release")
        focus = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            task["id"],
            now_ms=1_784_548_800_000,
        )
        self.assertEqual(focus["taskId"], task["id"])

        self.store.apply_sync(
            {
                "acknowledgements": [
                    {"commandId": focus["id"], "outcome": "applied", "reason": ""}
                ],
                "taskAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationAcknowledgements": [],
                "autoStartAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": False,
                "serverHlcWallMs": focus["hlcWallMs"],
                "serverHlcCounter": focus["hlcCounter"],
            },
            self.store.sync_payload(),
        )
        break_start = self.store.queue_command(
            "start",
            None,
            "short_break",
            settings["durationsMs"],
            task["id"],
            now_ms=1_784_548_801_000,
        )
        self.assertNotIn("taskId", break_start)

    def test_reset_clears_account_tasks_and_selection(self) -> None:
        task = task_from_title("Release")
        self.store.queue_task_operation("upsert", task, now_ms=1_784_548_800_000)
        self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=1_784_548_800_001
        )
        self.store.set_selected_task_id(task["id"])
        self.store.set_selected_phase("long_break")
        self.store.set_auto_start_breaks(True)

        self.store.reset_account_data()
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["pendingDurations"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [])
        self.assertIsNone(loaded["settings"]["selectedTaskId"])
        self.assertEqual(
            loaded["settings"]["durationsMs"],
            {
                "focus": 25 * 60_000,
                "short_break": 5 * 60_000,
                "long_break": 15 * 60_000,
            },
        )
        self.assertEqual(loaded["settings"]["selectedPhase"], "long_break")
        self.assertFalse(loaded["settings"]["autoStartBreaks"])

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.load()["pendingDurations"], [])


if __name__ == "__main__":
    unittest.main()
