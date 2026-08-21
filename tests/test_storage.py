from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from copy import deepcopy
from itertools import permutations
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
from pomodorough.storage import (
    MAX_CLOCK_SKEW_MS,
    MAX_SAFE_INTEGER,
    MAX_SERVER_TIME_UNCERTAINTY_MS,
    Store,
    default_data_path,
    utc_timestamp,
)


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

    def test_canonical_shipping_fixture_uses_production_sync_decoder(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures/protocol-fixtures-v1.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        decoded = self.store._validated_sync_response(
            fixture["syncResponse"], fixture["syncRequest"]
        )

        self.assertEqual(decoded["revision"], 5)
        self.assertEqual(
            decoded["canonicalTimer"]["id"],
            "01a0219e-0800-7002-8000-000000000002",
        )
        self.assertEqual([task["title"] for task in decoded["tasks"]], ["Ship release"])
        self.assertEqual(decoded["selectedTaskId"], None)
        for key in (
            "acknowledgements",
            "taskAcknowledgements",
            "durationAcknowledgements",
            "autoStartAcknowledgements",
            "selectedTaskAcknowledgements",
        ):
            self.assertEqual(len(decoded[key]), 1)

    def test_centralized_timer_ownership_survives_restart(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])

        self.assertTrue(self.store.owns_timer(running))
        self.store.close()
        self.store = Store(self.path)

        self.assertTrue(self.store.owns_timer(running))
        self.store.reset_account_data()
        self.assertIsNone(self.store.get_meta("centralizedTimerOwnership"))

    def test_canonical_replacement_clears_centralized_timer_ownership(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        local_timer, _history = rebuild_optimistic(None, [], [start])
        self.assertTrue(self.store.owns_timer(local_timer))
        request = self.store.sync_payload()
        remote_timer = {
            "id": "remote-timer",
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 25 * 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": utc_timestamp(2_000),
            "taskId": None,
        }
        response = self._canonical_response(request, revision=1)
        response["canonicalTimer"] = remote_timer

        self.store.apply_sync(response, request)

        self.assertIsNone(self.store.get_meta("centralizedTimerOwnership"))
        self.assertFalse(self.store.owns_timer(remote_timer))
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.owns_timer(remote_timer))

    @staticmethod
    def _history_item(
        item_id: str, completed_at: str = "2026-07-22T10:00:00.000Z"
    ) -> dict[str, object]:
        return {
            "id": item_id,
            "timerId": f"timer-{item_id}",
            "phase": "focus",
            "status": "completed",
            "plannedDurationMs": 25 * 60_000,
            "completedAt": completed_at,
        }

    @staticmethod
    def _operation_intent(operation: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in operation.items()
            if key not in {"occurredAt", "hlcWallMs", "hlcCounter"}
        }

    @staticmethod
    def _wire_preference_operation(
        operation: dict[str, object],
    ) -> dict[str, object]:
        return {key: value for key, value in operation.items() if key != "deviceId"}

    @classmethod
    def _wire_preference_operations(
        cls, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        return [cls._wire_preference_operation(operation) for operation in operations]

    @staticmethod
    def _canonical_response(
        request: dict[str, object],
        *,
        revision: int = 1,
        history: list[dict[str, object]] | None = None,
        tasks: list[dict[str, str]] | None = None,
        auto_start_breaks: bool | None = None,
        selected_task_id: str | None = None,
    ) -> dict[str, object]:
        auto_start_operations = request.get("autoStartOperations", [])
        if auto_start_breaks is None:
            auto_start_breaks = (
                auto_start_operations[-1]["enabled"]
                if auto_start_operations
                else False
            )
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
            "selectedTaskAcknowledgements": [
                {"operationId": item["id"], "outcome": "applied", "reason": ""}
                for item in request.get("selectedTaskOperations", [])
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
            "selectedTaskId": selected_task_id,
            "serverTime": utc_timestamp(wall_ms),
            "serverHlcWallMs": wall_ms,
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

    def _queue_offline_auto_break(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
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
        generated = self.store.process_auto_break(
            require_canonical=False, now_ms=3_000
        )[0]
        return start, finish, generated

    @staticmethod
    def _canonical_completion(
        commands: list[dict[str, object]],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        timer, history = rebuild_optimistic(None, [], commands)
        for item in history:
            item.pop("pending", None)
        return timer, history

    def test_bootstrap_resolution_strategy_requires_choice_before_discarding_state(self) -> None:
        empty_remote = self._canonical_response(
            {"commands": [], "taskOperations": [], "durationOperations": []},
            revision=7,
        )
        self.assertEqual(
            self.store.bootstrap_resolution_plan(empty_remote)["strategy"],
            "keep_remote",
        )

        self.store.queue_task_operation(
            "upsert", task_from_title("Local task"), now_ms=100
        )
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

        self.store.queue_task_operation("upsert", task_from_title("Local task"))
        remote_with_local_state = self.store.bootstrap_resolution_plan(
            remote_response
        )
        self.assertFalse(remote_with_local_state["localHistory"])
        self.assertTrue(remote_with_local_state["remoteHistory"])
        self.assertIsNone(remote_with_local_state["strategy"])

        self.store.reset_account_data()
        self.store.set_meta("hlc", {"wallMs": 0, "counter": 0})
        self._queue_completed_timer()
        remote_task_response = deepcopy(empty_remote)
        remote_task_response["tasks"] = [task_from_title("Remote task")]
        local_history_remote_task = self.store.bootstrap_resolution_plan(
            remote_task_response
        )
        self.assertTrue(local_history_remote_task["localHistory"])
        self.assertFalse(local_history_remote_task["remoteHistory"])
        self.assertIsNone(local_history_remote_task["strategy"])

    def test_bootstrap_meaningful_state_matrix_covers_settings_and_terminal_history(self) -> None:
        empty_remote = self._canonical_response(
            {"commands": [], "taskOperations": [], "durationOperations": []}
        )
        remote_history = deepcopy(empty_remote)
        remote_history["history"] = [self._history_item("remote")]

        for mutation in (
            lambda: self.store.set_auto_start_breaks(True, now_ms=100),
            lambda: self.store.queue_duration_operation(
                "focus", 30 * 60_000, now_ms=100
            ),
            lambda: self.store.queue_task_operation(
                "upsert", task_from_title("Local task"), now_ms=100
            ),
        ):
            mutation()
            self.assertIsNone(
                self.store.bootstrap_resolution_plan(remote_history)["strategy"]
            )
            self.store.reset_account_data()

        self._queue_completed_timer()
        for key, value in (
            ("autoStartBreaks", True),
            (
                "durationsMs",
                {
                    "focus": 30 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
            ),
            ("history", [{**self._history_item("remote-cancelled"), "status": "cancelled", "completedAt": None, "endedAt": "2026-07-22T10:00:00.000Z"}]),
        ):
            remote = deepcopy(empty_remote)
            remote[key] = value
            self.assertIsNone(
                self.store.bootstrap_resolution_plan(remote)["strategy"]
            )

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
                "selectedTaskOperations",
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

    def test_malformed_pending_resolution_blocks_without_changing_raw_state(
        self,
    ) -> None:
        variants = (
            ("structured", []),
            (
                "missing queue IDs",
                {
                    "owner": {"id": "user-1"},
                    "request": {
                        "requestId": "request-1",
                        "deviceId": self.store.device_id,
                        "strategy": "merge",
                    },
                },
            ),
            ("invalid JSON", None),
        )
        for label, value in variants:
            with self.subTest(label=label):
                self.store.reset_account_data()
                operation = self.store.queue_task_operation(
                    "upsert",
                    task_from_title(f"Preserved {label}"),
                    now_ms=1,
                )
                if label == "invalid JSON":
                    self.store.connection.execute(
                        "UPDATE meta SET value = ? WHERE key = ?",
                        ("not-json", "pendingResolution"),
                    )
                    self.store.connection.commit()
                else:
                    self.store.set_meta("pendingResolution", value)
                raw_before = self.store.connection.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    ("pendingResolution",),
                ).fetchone()["value"]

                with self.assertRaisesRegex(ValueError, "account history is corrupted"):
                    self.store.pending_resolution()
                with self.assertRaisesRegex(ValueError, "account history is corrupted"):
                    self.store.sync_payload()
                with self.assertRaisesRegex(ValueError, "account history is corrupted"):
                    self.store.set_selected_phase("long_break")

                raw_after = self.store.connection.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    ("pendingResolution",),
                ).fetchone()["value"]
                self.assertEqual(raw_after, raw_before)
                self.assertEqual(self.store.load()["pendingTasks"], [operation])
                self.assertIsNotNone(self.store.load()["pendingResolution"])

    def test_normal_sync_claim_blocks_resolution_until_exact_response_applies(
        self,
    ) -> None:
        task = task_from_title("Claimed sync response")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1, tasks=[task])
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "pending normal sync"):
            self.store.prepare_resolution({"id": "user-1"}, 0, "merge")

        self.assertEqual(self.store.load(), before)
        self.assertEqual(before["pendingTasks"], [operation])
        self.assertEqual(self.store.pending_sync(), request)
        self.assertIsNone(before["pendingResolution"])

        self.store.apply_sync(response, request)

        self.assertIsNone(self.store.pending_sync())
        self.assertEqual(self.store.load()["pendingTasks"], [])

    def test_normal_sync_and_resolution_claims_are_transactionally_exclusive(
        self,
    ) -> None:
        task = task_from_title("Concurrent normal and resolution claim")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        barrier = Barrier(3)
        results = Queue()

        def claim_normal_sync() -> None:
            store = Store(self.path)
            try:
                barrier.wait()
                results.put(("sync", "request", store.sync_payload()))
            except ValueError as error:
                results.put(("sync", "error", str(error)))
            finally:
                store.close()

        def claim_resolution() -> None:
            store = Store(self.path)
            try:
                barrier.wait()
                results.put(
                    (
                        "resolution",
                        "request",
                        store.prepare_resolution({"id": "user-1"}, 0, "merge"),
                    )
                )
            except ValueError as error:
                results.put(("resolution", "error", str(error)))
            finally:
                store.close()

        threads = (Thread(target=claim_normal_sync), Thread(target=claim_resolution))
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        outcomes = [results.get_nowait(), results.get_nowait()]
        successes = [outcome for outcome in outcomes if outcome[1] == "request"]
        errors = [outcome for outcome in outcomes if outcome[1] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        if successes[0][0] == "sync":
            self.assertEqual(self.store.pending_sync(), successes[0][2])
            self.assertIsNone(self.store.pending_resolution())
            self.assertIn("pending normal sync", errors[0][2])
        else:
            self.assertIsNone(self.store.pending_sync())
            self.assertEqual(
                self.store.pending_resolution()["request"],
                successes[0][2],
            )
            self.assertIn("pending account history", errors[0][2])

    def test_normal_sync_survives_every_restart_checkpoint(self) -> None:
        task = task_from_title("Durable normal sync")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        self.store.close()

        self.store = Store(self.path)
        self.assertEqual(self.store.sync_payload(), request)
        response = self._canonical_response(request, revision=1, tasks=[task])

        self.store.apply_sync(response, request)

        self.assertIsNone(self.store.pending_sync())
        self.assertEqual(self.store.load()["pendingTasks"], [])
        self.assertEqual(self.store.load()["snapshot"]["tasks"], [task])
        self.assertEqual(request["taskOperations"], [operation])

        self.store.close()
        self.store = Store(self.path)
        after_apply = self.store.sync_payload()
        self.assertEqual(after_apply["commands"], [])
        self.assertEqual(after_apply["taskOperations"], [])
        self.assertEqual(after_apply["durationOperations"], [])
        self.assertEqual(after_apply["autoStartOperations"], [])
        self.assertEqual(self.store.load()["snapshot"]["tasks"], [task])

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

    def test_delayed_sync_response_cannot_cross_destructive_account_switch(
        self,
    ) -> None:
        old_task = task_from_title("Old account task")
        old_operation = self.store.queue_task_operation(
            "upsert", old_task, now_ms=1
        )
        old_request = self.store.sync_payload()
        old_response = self._canonical_response(
            old_request, revision=99, tasks=[old_task]
        )

        self.store.reset_account_data()
        snapshot = self.store.load()["snapshot"]
        snapshot["user"] = {"id": "user-b"}
        self.store.set_meta("snapshot", snapshot)
        new_task = task_from_title("New account task")
        new_operation = self.store.queue_task_operation(
            "upsert", new_task, now_ms=2
        )
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "active normal sync claim"):
            self.store.apply_sync(old_response, old_request)

        self.assertEqual(self.store.load(), before)
        self.assertEqual(before["snapshot"]["user"], {"id": "user-b"})
        self.assertEqual(before["pendingTasks"], [new_operation])
        self.assertNotEqual(old_operation["id"], new_operation["id"])

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
                self.store.set_meta("hlc", {"wallMs": 0, "counter": 0})
                self.store.queue_task_operation("upsert", local_task, now_ms=1)
                self.store.queue_duration_operation("focus", 30 * 60_000, now_ms=2)
                self._queue_completed_timer()
                self.store.set_selected_phase("long_break")
                self.store.set_auto_start_breaks(True)
                request = self.store.prepare_resolution(user, 4, strategy)
                expected_counts = (
                    (0, 0, 0, 0, 0)
                    if strategy == "keep_remote"
                    else (2, 1, 1, 1, 0)
                )
                self.assertEqual(
                    (
                        len(request["commands"]),
                        len(request["taskOperations"]),
                        len(request["durationOperations"]),
                        len(request["autoStartOperations"]),
                        len(request["selectedTaskOperations"]),
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
                response["serverTime"] = utc_timestamp(
                    self.store.get_meta("hlc")["wallMs"]
                )
                response["serverHlcWallMs"] = self.store.get_meta("hlc")[
                    "wallMs"
                ]

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
                            "selectedTaskId": None,
                            "user": user,
                        },
                        "pending": [],
                        "pendingTasks": [],
                        "pendingDurations": [],
                        "pendingAutoStarts": [],
                        "pendingSelectedTasks": [],
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
                        "selectedTaskOperations": [],
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
                retained = (
                    (loaded["pending"], new_command),
                    (loaded["pendingTasks"], new_task),
                    (loaded["pendingDurations"], new_duration),
                )
                for operations, original in retained:
                    self.assertEqual(len(operations), 1)
                    self.assertEqual(
                        self._operation_intent(operations[0]),
                        self._operation_intent(original),
                    )
                    self.assertGreater(
                        (
                            operations[0]["hlcWallMs"],
                            operations[0]["hlcCounter"],
                        ),
                        (
                            response["serverHlcWallMs"],
                            response["serverHlcCounter"],
                        ),
                    )
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
                    json.dumps(
                        {
                            "id": f"operation-{index}",
                            "taskId": task_from_title(f"Limit task {index}")["id"],
                            "type": "delete",
                            "occurredAt": utc_timestamp(index + 1),
                            "hlcWallMs": index + 1,
                            "hlcCounter": 0,
                        },
                        separators=(",", ":"),
                    ),
                )
                for index in range(count)
            ]
            self.store.connection.executemany(
                "INSERT INTO pending_task_operations(id, payload) VALUES (?, ?)",
                rows,
            )
            self.store._set_meta("hlc", {"wallMs": count, "counter": 0})
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
            "selectedTaskAcknowledgements",
            "revision",
            "canonicalTimer",
            "history",
            "tasks",
            "durationsMs",
            "autoStartBreaks",
            "selectedTaskId",
            "serverTime",
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

    def test_sync_requires_selected_task_fields_even_without_selected_task_operations(self) -> None:
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.pop("selectedTaskAcknowledgements")
        response.pop("selectedTaskId")
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "canonical fields"):
            self.store.apply_sync(response, request)

        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.store.pending_sync(), request)

    def test_sync_revision_guard_rejects_lower_and_applies_equal_or_higher(self) -> None:
        for response_revision, accepted in ((4, False), (5, True), (6, True)):
            with self.subTest(revision=response_revision):
                self.store.reset_account_data()
                snapshot = self.store.get_meta("snapshot")
                snapshot["revision"] = 5
                self.store.set_meta("snapshot", snapshot)
                task = task_from_title(f"Revision {response_revision}")
                operation = self.store.queue_task_operation(
                    "upsert", task, now_ms=response_revision
                )
                request = self.store.sync_payload()
                response = self._canonical_response(
                    request, revision=response_revision, tasks=[task]
                )
                before = self.store.load()

                if accepted:
                    self.store.apply_sync(response, request)
                    loaded = self.store.load()
                    self.assertEqual(
                        loaded["snapshot"]["revision"], response_revision
                    )
                    self.assertEqual(loaded["snapshot"]["tasks"], [task])
                    self.assertEqual(loaded["pendingTasks"], [])
                else:
                    with self.assertRaisesRegex(ValueError, "regress canonical revision"):
                        self.store.apply_sync(response, request)
                    self.assertEqual(self.store.load(), before)
                    self.assertEqual(before["pendingTasks"], [operation])

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
                "selectedTaskAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "serverTime": queued["occurredAt"],
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
                "selectedTaskAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "autoStartBreaks": False,
                "selectedTaskId": None,
                "serverTime": operation["occurredAt"],
                "serverHlcWallMs": operation["hlcWallMs"],
                "serverHlcCounter": operation["hlcCounter"],
            },
            payload,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

    def test_selected_task_syncs_task_null_and_remote_pull(self) -> None:
        first = task_from_title("First focus task")
        second = task_from_title("Second focus task")
        for task, now_ms in ((first, 1_000), (second, 1_001)):
            self.store.queue_task_operation("upsert", task, now_ms=now_ms)
        request = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(
                request, revision=1, tasks=[first, second]
            ),
            request,
        )

        selected = self.store.set_selected_task_id(first["id"], now_ms=2_000)
        request = self.store.sync_payload()
        self.assertEqual(selected["deviceId"], self.store.device_id)
        self.assertEqual(
            request["selectedTaskOperations"],
            self._wire_preference_operations([selected]),
        )
        self.store.apply_sync(
            self._canonical_response(
                request,
                revision=2,
                tasks=[first, second],
                selected_task_id=first["id"],
            ),
            request,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingSelectedTasks"], [])
        self.assertEqual(loaded["settings"]["selectedTaskId"], first["id"])
        self.assertEqual(loaded["snapshot"]["selectedTaskId"], first["id"])

        cleared = self.store.set_selected_task_id(None, now_ms=3_000)
        request = self.store.sync_payload()
        self.assertEqual(
            request["selectedTaskOperations"],
            self._wire_preference_operations([cleared]),
        )
        self.store.apply_sync(
            self._canonical_response(
                request, revision=3, tasks=[first, second], selected_task_id=None
            ),
            request,
        )
        self.assertIsNone(self.store.load()["settings"]["selectedTaskId"])

        pull = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(
                pull,
                revision=4,
                tasks=[first, second],
                selected_task_id=second["id"],
            ),
            pull,
        )
        self.assertEqual(self.store.load()["settings"]["selectedTaskId"], second["id"])

    def test_selected_task_sync_replays_newer_in_flight_choice(self) -> None:
        task = task_from_title("In-flight focus task")
        self.store.queue_task_operation("upsert", task, now_ms=1_000)
        task_request = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(task_request, revision=1, tasks=[task]),
            task_request,
        )
        sent = self.store.set_selected_task_id(task["id"], now_ms=2_000)
        request = self.store.sync_payload()
        replacement = self.store.set_selected_task_id(None, now_ms=3_000)

        self.store.apply_sync(
            self._canonical_response(
                request,
                revision=2,
                tasks=[task],
                selected_task_id=task["id"],
            ),
            request,
        )

        loaded = self.store.load()
        self.assertEqual(
            request["selectedTaskOperations"],
            self._wire_preference_operations([sent]),
        )
        self.assertEqual(loaded["pendingSelectedTasks"], [replacement])
        self.assertIsNone(loaded["settings"]["selectedTaskId"])
        self.assertEqual(loaded["snapshot"]["selectedTaskId"], task["id"])

    def test_persisted_preference_claims_upgrade_to_wire_shape(self) -> None:
        task = task_from_title("Persisted wire preference")
        self.store.queue_task_operation("upsert", task, now_ms=1)
        self.store.set_selected_task_id(task["id"], now_ms=2)
        self.store.set_auto_start_breaks(True, now_ms=3)
        request = self.store.sync_payload()
        local = self.store.load()
        legacy_request = {
            **request,
            "autoStartOperations": local["pendingAutoStarts"],
            "selectedTaskOperations": local["pendingSelectedTasks"],
        }
        self.store.set_meta("pendingSync", legacy_request)
        self.store.close()

        self.store = Store(self.path)
        retry = self.store.sync_payload()
        self.assertEqual(
            retry["autoStartOperations"],
            self._wire_preference_operations(local["pendingAutoStarts"]),
        )
        self.assertEqual(
            retry["selectedTaskOperations"],
            self._wire_preference_operations(local["pendingSelectedTasks"]),
        )
        self.assertEqual(self.store.get_meta("pendingSync"), retry)

        self.store.set_meta("pendingSync", None)
        user = {"id": "user-1"}
        bootstrap_request = self.store.prepare_resolution(user, 0, "merge")
        pending = self.store.pending_resolution(user["id"])
        legacy_bootstrap_request = {
            **bootstrap_request,
            "autoStartOperations": local["pendingAutoStarts"],
            "selectedTaskOperations": local["pendingSelectedTasks"],
        }
        self.store.set_meta(
            "pendingResolution",
            {**pending, "request": legacy_bootstrap_request},
        )
        self.store.close()

        self.store = Store(self.path)
        bootstrap_retry = self.store.prepare_resolution(user, 99, "keep_remote")
        self.assertEqual(
            bootstrap_retry["autoStartOperations"],
            self._wire_preference_operations(local["pendingAutoStarts"]),
        )
        self.assertEqual(
            bootstrap_retry["selectedTaskOperations"],
            self._wire_preference_operations(local["pendingSelectedTasks"]),
        )
        self.assertEqual(
            self.store.pending_resolution(user["id"])["request"], bootstrap_retry
        )

    def test_selected_task_response_validation_is_atomic(self) -> None:
        task = task_from_title("Strict focus task")
        operation = self.store.set_selected_task_id(task["id"], now_ms=1_000)
        request = self.store.sync_payload()
        response = self._canonical_response(
            request,
            revision=1,
            tasks=[task],
            selected_task_id=task["id"],
        )
        before = self.store.load()

        malformed_ack = deepcopy(response)
        malformed_ack["selectedTaskAcknowledgements"] = []
        with self.assertRaisesRegex(ValueError, "selected-task acknowledgement"):
            self.store.apply_sync(malformed_ack, request)
        self.assertEqual(self.store.load(), before)

        malformed_selection = deepcopy(response)
        malformed_selection["selectedTaskId"] = "missing-task"
        with self.assertRaisesRegex(ValueError, "selected-task preference"):
            self.store.apply_sync(malformed_selection, request)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(before["pendingSelectedTasks"], [operation])

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

    def test_task_sync_covers_protocol_batch_partitions(self) -> None:
        cases = {
            1: [1],
            255: [255],
            256: [256],
            257: [256, 1],
            513: [256, 256, 1],
        }
        for operation_count, expected_batch_sizes in cases.items():
            with self.subTest(operation_count=operation_count):
                with tempfile.TemporaryDirectory() as root:
                    store = Store(Path(root) / "batch.sqlite3")
                    try:
                        tasks = [
                            task_from_title(
                                f"Batch {operation_count}-{index:03d}"
                            )
                            for index in range(operation_count)
                        ]
                        for index, task in enumerate(tasks):
                            store.queue_task_operation(
                                "upsert", task, now_ms=index + 1
                            )

                        batch_sizes = []
                        applied_count = 0
                        while applied_count < operation_count:
                            request = store.sync_payload()
                            batch_size = len(request["taskOperations"])
                            batch_sizes.append(batch_size)
                            applied_count += batch_size
                            store.apply_sync(
                                self._canonical_response(
                                    request,
                                    revision=len(batch_sizes),
                                    tasks=tasks[:applied_count],
                                ),
                                request,
                            )

                        self.assertEqual(batch_sizes, expected_batch_sizes)
                        loaded = store.load()
                        self.assertEqual(loaded["pendingTasks"], [])
                        self.assertEqual(
                            loaded["snapshot"]["tasks"], tasks
                        )
                    finally:
                        store.close()

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
        task = task_from_title("Concurrent selected task")
        other = Store(self.path)
        try:
            other.queue_duration_operation("focus", 30 * 60_000, now_ms=1_000)
            other.set_selected_task_id(task["id"], now_ms=1_001)
        finally:
            other.close()

        stale["autoStartBreaks"] = True
        self.store.save_settings(stale)

        settings = self.store.load()["settings"]
        self.assertEqual(settings["durationsMs"]["focus"], 30 * 60_000)
        self.assertFalse(settings["autoStartBreaks"])
        self.assertEqual(settings["selectedTaskId"], task["id"])

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
            "selectedTaskAcknowledgements": [],
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
            "selectedTaskId": None,
            "serverTime": utc_timestamp(1_000),
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
            "selectedTaskAcknowledgements": [],
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
            "selectedTaskId": None,
            "serverTime": utc_timestamp(server_wall),
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
                        "focus", 27 * 60_000, now_ms=server_wall
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

    def test_trusted_time_accepts_exact_skew_and_rejects_one_past_atomically(
        self,
    ) -> None:
        now_ms = 1_800_000_000_000
        settings = self.store.load()["settings"]
        self.store.set_meta(
            "hlc", {"wallMs": now_ms + MAX_CLOCK_SKEW_MS, "counter": 7}
        )

        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms
        )
        self.assertEqual(
            (command["hlcWallMs"], command["hlcCounter"]),
            (now_ms + MAX_CLOCK_SKEW_MS, 8),
        )

        self.store.reset_account_data()
        self.store.set_meta(
            "hlc", {"wallMs": now_ms + MAX_CLOCK_SKEW_MS + 1, "counter": 7}
        )
        before = self.store.load()
        with self.assertRaisesRegex(ValueError, "trusted-time limit"):
            self.store.queue_task_operation(
                "upsert", task_from_title("Blocked skew"), now_ms=now_ms
            )
        self.assertEqual(self.store.load(), before)

    def test_server_time_midpoint_persists_offset_and_survives_wall_jumps(self) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )

        self.store.apply_sync(
            response,
            request,
            request_physical_ms=server_ms + 3_600_000,
            received_physical_ms=server_ms + 3_600_100,
            request_monotonic_ms=10_000,
            received_monotonic_ms=10_100,
        )

        self.assertEqual(
            self.store.get_meta("serverClockSample"),
            {
                "offsetMs": -3_600_050,
                "uncertaintyMs": 50,
                "acquiredPhysicalMs": server_ms + 3_600_100,
                "acquiredMonotonicMs": 10_100,
                "acquiredTrustedMs": server_ms + 50,
            },
        )
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 7_200_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns",
                return_value=11_100_000_000,
            ),
        ):
            first = self.store.queue_task_operation(
                "upsert", task_from_title("Forward wall jump")
            )
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms - 7_200_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns",
                return_value=12_100_000_000,
            ),
        ):
            second = self.store.queue_task_operation(
                "upsert", task_from_title("Backward wall jump")
            )

        self.assertEqual(first["occurredAt"], utc_timestamp(server_ms + 1_050))
        self.assertEqual(second["occurredAt"], utc_timestamp(server_ms + 2_050))
        self.assertEqual(first["hlcWallMs"], server_ms + 1_050)
        self.assertEqual(second["hlcWallMs"], server_ms + 2_050)

    def test_restart_reuses_persisted_offset_then_anchors_monotonic_time(self) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )
        self.store.apply_sync(
            response,
            request,
            request_physical_ms=server_ms + 3_600_000,
            received_physical_ms=server_ms + 3_600_100,
            request_monotonic_ms=10_000,
            received_monotonic_ms=10_100,
        )
        self.store.close()
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 3_601_100) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=11_100_000_000
            ),
        ):
            self.store = Store(self.path)
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 3_602_100) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=12_100_000_000
            ),
        ):
            first = self.store.queue_task_operation(
                "upsert", task_from_title("Restart offset")
            )
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 3_603_100) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=13_100_000_000
            ),
        ):
            second = self.store.queue_task_operation(
                "upsert", task_from_title("Restart monotonic")
            )

        self.assertEqual(first["occurredAt"], utc_timestamp(server_ms + 2_050))
        self.assertEqual(second["occurredAt"], utc_timestamp(server_ms + 3_050))

    def test_restart_invalidates_sample_after_unverifiable_wall_discontinuity(
        self,
    ) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )
        self.store.apply_sync(
            response,
            request,
            request_physical_ms=server_ms + 3_600_000,
            received_physical_ms=server_ms + 3_600_100,
            request_monotonic_ms=10_000,
            received_monotonic_ms=10_100,
        )
        self.store.close()

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 1_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=11_100_000_000
            ),
        ):
            self.store = Store(self.path)

        self.assertIsNone(self.store.get_meta("serverClockSample"))
        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(server_ms + 2_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=12_100_000_000
            ),
        ):
            operation = self.store.queue_task_operation(
                "upsert", task_from_title("Restart local fallback")
            )
        self.assertEqual(operation["occurredAt"], utc_timestamp(server_ms + 2_000))

    def test_server_time_accepts_one_hour_device_skew_and_projects_ui_time(self) -> None:
        server_ms = 1_800_000_000_000
        for index, device_skew_ms in enumerate((-3_600_000, 3_600_000), start=1):
            with self.subTest(device_skew_ms=device_skew_ms):
                path = Path(self.temporary.name) / f"skew-{index}.sqlite3"
                store = Store(path)
                try:
                    request = store.sync_payload()
                    response = self._canonical_response(request, revision=1)
                    response.update(
                        serverTime=utc_timestamp(server_ms),
                        serverHlcWallMs=server_ms,
                        canonicalTimer={
                            "id": f"skew-timer-{index}",
                            "phase": "focus",
                            "status": "running",
                            "plannedDurationMs": 25 * 60_000,
                            "elapsedAtAnchorMs": 0,
                            "anchorAt": utc_timestamp(server_ms),
                        },
                    )
                    store.apply_sync(
                        response,
                        request,
                        request_physical_ms=server_ms + device_skew_ms,
                        received_physical_ms=server_ms + device_skew_ms + 100,
                        request_monotonic_ms=10_000,
                        received_monotonic_ms=10_100,
                    )

                    sample = store.get_meta("serverClockSample")
                    self.assertEqual(sample["offsetMs"], -device_skew_ms - 50)
                    self.assertEqual(sample["uncertaintyMs"], 50)
                    loaded = store.load(projection=True)
                    self.assertEqual(
                        loaded["snapshot"]["canonicalTimer"]["anchorAt"],
                        utc_timestamp(server_ms),
                    )
                    self.assertEqual(
                        loaded["projectionSnapshot"]["canonicalTimer"]["anchorAt"],
                        utc_timestamp(server_ms + device_skew_ms + 50),
                    )
                finally:
                    store.close()

    def test_server_time_rejects_excess_uncertainty_and_invalid_hlc_atomically(
        self,
    ) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "uncertainty"):
            self.store.apply_sync(
                response,
                request,
                request_physical_ms=server_ms,
                received_physical_ms=server_ms + 60_001,
                request_monotonic_ms=0,
                received_monotonic_ms=MAX_SERVER_TIME_UNCERTAINTY_MS * 2 + 1,
            )
        self.assertEqual(self.store.load(), before)
        self.assertIsNone(self.store.get_meta("serverClockSample"))

        for invalid_wall_ms in (server_ms - 1, server_ms + MAX_CLOCK_SKEW_MS + 1):
            with self.subTest(invalid_wall_ms=invalid_wall_ms):
                invalid = deepcopy(response)
                invalid["serverHlcWallMs"] = invalid_wall_ms
                with self.assertRaisesRegex(ValueError, "logical clock"):
                    self.store.apply_sync(invalid, request)
                self.assertEqual(self.store.load(), before)

    def test_server_time_rejects_disagreeing_wall_and_monotonic_rtt(self) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )

        with self.assertRaisesRegex(ValueError, "clocks disagree"):
            self.store.apply_sync(
                response,
                request,
                request_physical_ms=server_ms,
                received_physical_ms=server_ms + 5_000,
                request_monotonic_ms=10_000,
                received_monotonic_ms=10_100,
            )

        self.assertIsNone(self.store.get_meta("serverClockSample"))

    def test_server_time_includes_endpoint_disagreement_in_uncertainty(self) -> None:
        server_ms = 1_800_000_000_000
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )

        self.store.apply_sync(
            response,
            request,
            request_physical_ms=server_ms,
            received_physical_ms=server_ms + 600,
            request_monotonic_ms=10_000,
            received_monotonic_ms=10_100,
        )

        sample = self.store.get_meta("serverClockSample")
        self.assertEqual(sample["offsetMs"], -50)
        self.assertEqual(sample["uncertaintyMs"], 550)

    def test_fresh_server_sample_can_regress_projected_trusted_time(self) -> None:
        server_ms = 1_800_000_000_000
        first_request = self.store.sync_payload()
        first = self._canonical_response(first_request, revision=1)
        first.update(
            serverTime=utc_timestamp(server_ms),
            serverHlcWallMs=server_ms,
        )
        self.store.apply_sync(
            first,
            first_request,
            request_physical_ms=server_ms + 3_600_000,
            received_physical_ms=server_ms + 3_600_100,
            request_monotonic_ms=10_000,
            received_monotonic_ms=10_100,
        )
        second_request = self.store.sync_payload()
        second = self._canonical_response(second_request, revision=2)
        second.update(
            serverTime=utc_timestamp(server_ms + 500),
            serverHlcWallMs=server_ms + 500,
        )

        self.store.apply_sync(
            second,
            second_request,
            request_physical_ms=server_ms + 3_601_000,
            received_physical_ms=server_ms + 3_601_100,
            request_monotonic_ms=11_000,
            received_monotonic_ms=11_100,
        )

        self.assertEqual(self.store.load()["snapshot"]["revision"], 2)
        self.assertEqual(
            self.store.get_meta("serverClockSample"),
            {
                "offsetMs": -3_600_550,
                "uncertaintyMs": 50,
                "acquiredPhysicalMs": server_ms + 3_601_100,
                "acquiredMonotonicMs": 11_100,
                "acquiredTrustedMs": server_ms + 550,
            },
        )

    def test_fresh_sample_preserves_running_timer_monotonic_continuity(self) -> None:
        physical_ms = 1_800_000_000_000
        with (
            patch("pomodorough.storage.time.time", return_value=physical_ms / 1_000),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=10_000_000_000
            ),
        ):
            settings = self.store.load()["settings"]
            start = self.store.queue_command(
                "start", None, "focus", settings["durationsMs"]
            )
            running, _history = rebuild_optimistic(None, [], [start])

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms + 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=40_000_000_000
            ),
        ):
            before = self.store.effective_timer_now_ms(running)
            request = self.store.sync_payload()
            response = self._canonical_response(request, revision=1)
            response.update(
                serverTime=utc_timestamp(physical_ms + 30_000),
                serverHlcWallMs=physical_ms + 30_000,
            )
            self.store.apply_sync(
                response,
                request,
                request_physical_ms=physical_ms + 3_600_000,
                received_physical_ms=physical_ms + 3_600_100,
                request_monotonic_ms=40_000,
                received_monotonic_ms=40_100,
            )
            after = self.store.effective_timer_now_ms(running)

        self.assertEqual(after, before)

    def test_active_timer_observed_elapsed_uses_monotonic_effective_now(self) -> None:
        physical_ms = 1_800_000_000_000
        with (
            patch(
                "pomodorough.storage.time.time", return_value=physical_ms / 1_000
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=10_000_000_000
            ),
        ):
            settings = self.store.load()["settings"]
            start = self.store.queue_command(
                "start", None, "focus", settings["durationsMs"]
            )
            running, _history = rebuild_optimistic(None, [], [start])

        with (
            patch(
                "pomodorough.storage.time.time",
                return_value=(physical_ms - 3_600_000) / 1_000,
            ),
            patch(
                "pomodorough.storage.time.monotonic_ns", return_value=40_000_000_000
            ),
        ):
            pause = self.store.queue_command(
                "pause", running, "focus", settings["durationsMs"]
            )

        self.assertEqual(pause["observedElapsedMs"], 30_000)

    def test_queue_restart_rejects_stale_terminal_projection_atomically(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        completed, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        other = Store(self.path)
        try:
            other.queue_restart(
                completed,
                "focus",
                settings["durationsMs"],
                now_ms=3_000,
            )
        finally:
            other.close()
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "Timer changed"):
            self.store.queue_restart(
                completed,
                "focus",
                settings["durationsMs"],
                now_ms=4_000,
            )

        self.assertEqual(self.store.load(), before)

    def test_cancel_and_clear_is_atomic_ordered_and_restart_safe(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])

        commands = self.store.queue_cancel_and_clear(
            running, "focus", settings["durationsMs"], now_ms=31_000
        )

        self.assertEqual([command["type"] for command in commands], ["cancel", "clear"])
        self.assertEqual(
            [command["deviceSequence"] for command in commands],
            [start["deviceSequence"] + 1, start["deviceSequence"] + 2],
        )
        self.assertLess(
            (commands[0]["hlcWallMs"], commands[0]["hlcCounter"]),
            (commands[1]["hlcWallMs"], commands[1]["hlcCounter"]),
        )
        self.assertEqual(commands[0]["occurredAt"], commands[1]["occurredAt"])
        self.assertEqual(commands[0]["observedElapsedMs"], 30_000)
        self.assertEqual(commands[1]["observedElapsedMs"], 30_000)

        reopened = Store(self.path)
        try:
            stored = reopened.load()
            timer, history = rebuild_optimistic(
                stored["snapshot"].get("canonicalTimer"),
                stored["snapshot"].get("history", []),
                stored["pending"],
            )
        finally:
            reopened.close()
        self.assertIsNone(timer)
        self.assertEqual([item["status"] for item in history], ["cancelled"])
        self.assertEqual(
            [command["type"] for command in stored["pending"]],
            ["start", "cancel", "clear"],
        )

    def test_cancel_and_clear_rejects_stale_projection_atomically(self) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        self.store.queue_command(
            "pause", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "Timer changed"):
            self.store.queue_cancel_and_clear(
                running, "focus", settings["durationsMs"], now_ms=3_000
            )

        self.assertEqual(self.store.load(), before)

    def test_sync_merged_hlc_covers_retained_in_flight_operation(self) -> None:
        sent = self.store.queue_task_operation(
            "upsert", task_from_title("Sent"), now_ms=1_000
        )
        request = self.store.sync_payload()
        retained = self.store.queue_task_operation(
            "upsert", task_from_title("Retained"), now_ms=2_000
        )
        response = self._canonical_response(
            request, revision=1, tasks=[task_from_title("Sent")]
        )

        self.store.apply_sync(response, request)

        self.assertEqual(request["taskOperations"], [sent])
        self.assertEqual(self.store.load()["pendingTasks"], [retained])
        self.assertGreaterEqual(
            (
                self.store.get_meta("hlc")["wallMs"],
                self.store.get_meta("hlc")["counter"],
            ),
            (retained["hlcWallMs"], retained["hlcCounter"]),
        )
        self.assertEqual(self.store.sync_payload()["taskOperations"], [retained])

    def test_sync_rebases_every_retained_domain_after_canonical_clock(self) -> None:
        sent_task = task_from_title("Sent canonical task")
        sent = self.store.queue_task_operation("upsert", sent_task, now_ms=1_000)
        request = self.store.sync_payload()
        settings = self.store.load()["settings"]
        retained_command = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            now_ms=2_000,
        )
        retained_task = self.store.queue_task_operation(
            "upsert",
            task_from_title("Retained task"),
            now_ms=2_001,
        )
        retained_duration = self.store.queue_duration_operation(
            "focus",
            30 * 60_000,
            now_ms=2_002,
        )
        retained_auto_start = self.store.set_auto_start_breaks(
            True,
            now_ms=2_003,
        )
        response = self._canonical_response(
            request,
            revision=1,
            tasks=[sent_task],
            auto_start_breaks=False,
        )
        response.update(
            serverTime=utc_timestamp(250_000),
            serverHlcWallMs=250_000,
            serverHlcCounter=10,
        )

        self.store.apply_sync(response, request)

        loaded = self.store.load()
        retained = (
            loaded["pending"]
            + loaded["pendingTasks"]
            + loaded["pendingDurations"]
            + loaded["pendingAutoStarts"]
        )
        self.assertEqual(
            {item["id"] for item in retained},
            {
                retained_command["id"],
                retained_task["id"],
                retained_duration["id"],
                retained_auto_start["id"],
            },
        )
        self.assertTrue(
            all(
                (item["hlcWallMs"], item["hlcCounter"]) > (250_000, 10)
                for item in retained
            )
        )
        self.assertEqual(
            {item["id"]: item["occurredAt"] for item in retained},
            {
                retained_command["id"]: retained_command["occurredAt"],
                retained_task["id"]: retained_task["occurredAt"],
                retained_duration["id"]: retained_duration["occurredAt"],
                retained_auto_start["id"]: retained_auto_start["occurredAt"],
            },
        )
        self.assertEqual(
            self.store.get_meta("commandPhysicalTimes")[retained_command["id"]],
            2_000,
        )
        self.assertEqual(request["taskOperations"], [sent])
        next_request = self.store.sync_payload()
        self.assertEqual(
            {
                item["id"]
                for key in (
                    "commands",
                    "taskOperations",
                    "durationOperations",
                    "autoStartOperations",
                )
                for item in next_request[key]
            },
            {item["id"] for item in retained},
        )
        self.assertEqual(
            loaded["settings"]["durationsMs"]["focus"],
            retained_duration["durationMs"],
        )
        self.assertTrue(loaded["settings"]["autoStartBreaks"])

    def test_sync_rolls_back_when_retained_operation_exceeds_trusted_time(self) -> None:
        sent = self.store.queue_task_operation(
            "upsert", task_from_title("Sent rollback"), now_ms=1_000
        )
        request = self.store.sync_payload()
        retained = self.store.queue_task_operation(
            "upsert", task_from_title("Retained future"), now_ms=2_000
        )
        future = dict(retained)
        future.update(
            occurredAt=utc_timestamp(1_000 + MAX_CLOCK_SKEW_MS + 1),
            hlcWallMs=1_000 + MAX_CLOCK_SKEW_MS + 1,
        )
        self.store.connection.execute(
            "UPDATE pending_task_operations SET payload = ? WHERE id = ?",
            (json.dumps(future, separators=(",", ":")), retained["id"]),
        )
        self.store._set_meta(
            "hlc", {"wallMs": future["hlcWallMs"], "counter": 0}
        )
        self.store.connection.commit()
        response = self._canonical_response(request, revision=1)
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "Retained pending operation"):
            self.store.apply_sync(
                response,
                request,
                request_physical_ms=1_000,
                received_physical_ms=1_000,
                request_monotonic_ms=0,
                received_monotonic_ms=0,
            )

        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.store.sync_payload()["taskOperations"][0], sent)

    def test_persisted_server_time_uncertainty_is_validated(self) -> None:
        self.store.set_meta(
            "serverClockSample",
            {
                "offsetMs": 0,
                "uncertaintyMs": MAX_SERVER_TIME_UNCERTAINTY_MS + 1,
            },
        )

        with self.assertRaisesRegex(ValueError, "uncertainty"):
            self.store.sync_payload()

    def test_single_generator_exact_max_bounds_and_overflow_are_atomic(self) -> None:
        now_ms = 1_800_000_000_000
        settings = self.store.load()["settings"]
        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER - 1)
        self.store.set_meta(
            "hlc", {"wallMs": now_ms, "counter": MAX_SAFE_INTEGER - 1}
        )

        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms
        )
        self.assertEqual(command["deviceSequence"], MAX_SAFE_INTEGER)
        self.assertEqual(command["hlcCounter"], MAX_SAFE_INTEGER)

        before = self.store.load()
        with self.assertRaisesRegex(ValueError, "headroom"):
            self.store.queue_command(
                "start", None, "focus", settings["durationsMs"], now_ms=now_ms
            )
        self.assertEqual(self.store.load(), before)

    def test_clock_counter_overflow_blocks_every_operation_generator(self) -> None:
        now_ms = 1_800_000_000_000
        settings = self.store.load()["settings"]
        task = task_from_title("Counter overflow")
        generators = (
            lambda: self.store.queue_command(
                "start", None, "focus", settings["durationsMs"], now_ms=now_ms
            ),
            lambda: self.store.queue_task_operation("upsert", task, now_ms=now_ms),
            lambda: self.store.queue_duration_operation(
                "focus", 30 * 60_000, now_ms=now_ms
            ),
            lambda: self.store.set_auto_start_breaks(True, now_ms=now_ms),
        )
        for generator in generators:
            with self.subTest(generator=generator):
                self.store.set_meta(
                    "hlc", {"wallMs": now_ms, "counter": MAX_SAFE_INTEGER}
                )
                before = self.store.load()
                with self.assertRaisesRegex(ValueError, "counter.*headroom"):
                    generator()
                self.assertEqual(self.store.load(), before)

    def test_restart_reserves_two_sequences_and_clocks_before_any_write(self) -> None:
        now_ms = 1_800_000_000_000
        settings = self.store.load()["settings"]
        started = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms
        )
        running, _history = rebuild_optimistic(None, [], [started])
        finished = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=now_ms
        )
        completed, _history = rebuild_optimistic(None, [], [started, finished])
        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER - 1)
        self.store.set_meta(
            "hlc", {"wallMs": now_ms, "counter": MAX_SAFE_INTEGER - 2}
        )
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "sequence.*headroom"):
            self.store.queue_restart(
                completed, "focus", settings["durationsMs"], now_ms=now_ms
            )
        self.assertEqual(self.store.load(), before)

        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER - 2)
        commands = self.store.queue_restart(
            completed, "focus", settings["durationsMs"], now_ms=now_ms
        )
        self.assertEqual(
            [command["deviceSequence"] for command in commands],
            [MAX_SAFE_INTEGER - 1, MAX_SAFE_INTEGER],
        )
        self.assertEqual(
            [command["hlcCounter"] for command in commands],
            [MAX_SAFE_INTEGER - 1, MAX_SAFE_INTEGER],
        )

    def test_finish_and_generated_break_reserve_two_slots_atomically(self) -> None:
        now_ms = 1_800_000_000_000
        self.store.set_auto_start_breaks(True, now_ms=now_ms - 2)
        settings = self.store.load()["settings"]
        started = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=now_ms - 1
        )
        running, _history = rebuild_optimistic(None, [], [started])
        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER - 1)
        self.store.set_meta("hlc", {"wallMs": now_ms, "counter": 0})
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "sequence.*headroom"):
            self.store.queue_command(
                "finish",
                running,
                "focus",
                settings["durationsMs"],
                now_ms=now_ms,
                generate_auto_break=True,
            )
        self.assertEqual(self.store.load(), before)

        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER - 2)
        finish = self.store.queue_command(
            "finish",
            running,
            "focus",
            settings["durationsMs"],
            now_ms=now_ms,
            generate_auto_break=True,
        )
        commands = self.store.load()["pending"][-2:]
        self.assertEqual([item["type"] for item in commands], ["finish", "start"])
        self.assertEqual(commands[0], finish)
        self.assertEqual(
            [item["deviceSequence"] for item in commands],
            [MAX_SAFE_INTEGER - 1, MAX_SAFE_INTEGER],
        )

    def test_provisional_auto_break_overflow_preserves_trigger_and_settings(self) -> None:
        _start, _finish, _generated = self._queue_offline_auto_break()
        self.store.reset_account_data()
        self.store.set_auto_start_breaks(True, now_ms=100)
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.store.set_meta("deviceSequence", MAX_SAFE_INTEGER)
        before = self.store.load()

        with self.assertRaisesRegex(ValueError, "sequence.*headroom"):
            self.store.process_auto_break(require_canonical=False, now_ms=3_000)
        self.assertEqual(self.store.load(), before)
        self.assertTrue(self.store.has_pending_auto_break())

    def test_sync_preflight_rejects_corruption_in_every_pending_queue(self) -> None:
        task = task_from_title("Corrupt queue")
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        task_operation = self.store.queue_task_operation("upsert", task, now_ms=1_001)
        duration = self.store.queue_duration_operation(
            "focus", 30 * 60_000, now_ms=1_002
        )
        auto_start = self.store.set_auto_start_breaks(True, now_ms=1_003)
        cases = (
            ("pending_commands", command["id"], "type", "unknown"),
            ("pending_task_operations", task_operation["id"], "hlcCounter", -1),
            ("pending_duration_operations", duration["id"], "hlcWallMs", 0),
            (
                "pending_auto_start_operations",
                auto_start["id"],
                "deviceId",
                "other-device",
            ),
        )
        for table, row_id, key, value in cases:
            with self.subTest(table=table):
                row = self.store.connection.execute(
                    f"SELECT payload FROM {table} WHERE id = ?", (row_id,)
                ).fetchone()
                original = row["payload"]
                corrupt = json.loads(original)
                corrupt[key] = value
                self.store.connection.execute(
                    f"UPDATE {table} SET payload = ? WHERE id = ?",
                    (json.dumps(corrupt, separators=(",", ":")), row_id),
                )
                self.store.connection.commit()
                before = self.store.load()
                with self.assertRaises(ValueError):
                    self.store.sync_payload()
                self.assertEqual(self.store.load(), before)
                self.store.connection.execute(
                    f"UPDATE {table} SET payload = ? WHERE id = ?",
                    (original, row_id),
                )
                self.store.connection.commit()

    def test_preflight_limits_legacy_epoch_sentinel_to_duration_and_auto_start(
        self,
    ) -> None:
        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        corrupt = dict(command)
        corrupt.update(
            occurredAt=utc_timestamp(0),
            hlcWallMs=0,
            hlcCounter=0,
        )
        self.store.connection.execute(
            "UPDATE pending_commands SET payload = ? WHERE id = ?",
            (json.dumps(corrupt, separators=(",", ":")), command["id"]),
        )
        self.store.connection.commit()
        with self.assertRaises(ValueError):
            self.store.sync_payload()

        self.store.reset_account_data()
        settings = self.store._normalize_settings(self.store.get_meta("settings", {}))
        with self.store._immediate_transaction():
            duration = self.store._queue_duration_operation(
                "focus", 30 * 60_000, settings, 0, bootstrap=True
            )
            auto_start = self.store._queue_auto_start_operation(
                True, settings, 0, bootstrap=True
            )
        payload = self.store.sync_payload()
        self.assertEqual(payload["durationOperations"], [duration])
        self.assertEqual(
            payload["autoStartOperations"],
            self._wire_preference_operations([auto_start]),
        )

    def test_load_reads_one_snapshot_while_sync_commits(self) -> None:
        task = task_from_title("Atomic load")
        operation = self.store.queue_task_operation("upsert", task, now_ms=1)
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1, tasks=[task])
        reached_pending = Event()
        continue_load = Event()
        results = Queue()
        errors = Queue()

        class PausingStore(Store):
            def _pending_commands(
                self, *, sendable_only: bool = False
            ) -> list[dict[str, object]]:
                pending = super()._pending_commands(sendable_only=sendable_only)
                reached_pending.set()
                continue_load.wait()
                return pending

        def load_from_second_connection() -> None:
            store = PausingStore(self.path)
            try:
                results.put(store.load())
            except BaseException as error:
                errors.put(error)
            finally:
                store.close()

        thread = Thread(target=load_from_second_connection)
        thread.start()
        try:
            self.assertTrue(reached_pending.wait(2))
            self.store.apply_sync(response, request)
        finally:
            continue_load.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(errors.empty(), list(errors.queue))
        loaded_during_commit = results.get_nowait()
        self.assertEqual(loaded_during_commit["snapshot"]["revision"], 0)
        self.assertEqual(loaded_during_commit["pendingTasks"], [operation])
        loaded_after_commit = self.store.load()
        self.assertEqual(loaded_after_commit["snapshot"]["revision"], 1)
        self.assertEqual(loaded_after_commit["pendingTasks"], [])

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
            "selectedTaskAcknowledgements": [],
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
            "selectedTaskId": None,
            "serverTime": utc_timestamp(1_000),
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }

        invalid_sets = (
            ("acknowledgements", "command", "commandId"),
            ("taskAcknowledgements", "task", "operationId"),
            ("durationAcknowledgements", "duration", "operationId"),
            ("autoStartAcknowledgements", "auto-start", "operationId"),
        )
        for key, label, id_key in invalid_sets:
            wrong_id = deepcopy(response[key])
            wrong_id[0][id_key] = "foreign-id"
            for invalid in ([], [*response[key], *response[key]], wrong_id):
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

    def test_all_ack_domains_accept_reordered_exact_sets_and_every_outcome(self) -> None:
        settings = self.store.load()["settings"]
        commands = [
            self.store.queue_command(
                "start",
                None,
                phase,
                settings["durationsMs"],
                now_ms=100 + index,
            )
            for index, phase in enumerate(("focus", "short_break", "long_break"))
        ]
        tasks = [task_from_title(f"Ack task {index}") for index in range(3)]
        task_operations = [
            self.store.queue_task_operation("upsert", task, now_ms=200 + index)
            for index, task in enumerate(tasks)
        ]
        duration_operations = [
            self.store.queue_duration_operation(
                phase, (index + 2) * 60_000, now_ms=300 + index
            )
            for index, phase in enumerate(("focus", "short_break", "long_break"))
        ]
        auto_start_operations = [
            self.store.set_auto_start_breaks(bool(index % 2), now_ms=400 + index)
            for index in range(3)
        ]
        request = self.store.sync_payload()
        self.assertEqual(request["commands"], commands)
        self.assertEqual(request["taskOperations"], task_operations)
        self.assertEqual(request["durationOperations"], duration_operations)
        self.assertEqual(
            request["autoStartOperations"],
            self._wire_preference_operations(auto_start_operations),
        )
        response = self._canonical_response(request, revision=1)
        outcomes = ("applied", "ignored", "rejected")
        domains = (
            ("acknowledgements", "commands", "commandId", "command"),
            (
                "taskAcknowledgements",
                "taskOperations",
                "operationId",
                "task",
            ),
            (
                "durationAcknowledgements",
                "durationOperations",
                "operationId",
                "duration",
            ),
            (
                "autoStartAcknowledgements",
                "autoStartOperations",
                "operationId",
                "auto-start",
            ),
        )
        expected_notices = []
        for response_key, request_key, id_key, label in domains:
            response[response_key] = []
            for item, outcome in zip(reversed(request[request_key]), outcomes):
                reason = "" if outcome == "applied" else f"{label} {outcome}"
                response[response_key].append(
                    {id_key: item["id"], "outcome": outcome, "reason": reason}
                )
                if reason:
                    expected_notices.append(reason)

        notices = self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertEqual(notices, expected_notices)
        self.assertEqual(loaded["pending"], [])
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["pendingDurations"], [])
        self.assertEqual(loaded["pendingAutoStarts"], [])

    def test_duration_projection_is_permutation_invariant_and_idempotent(self) -> None:
        canonical = {
            "focus": 40 * 60_000,
            "short_break": 8 * 60_000,
            "long_break": 20 * 60_000,
        }
        projected = {
            "focus": 30 * 60_000,
            "short_break": 6 * 60_000,
            "long_break": 16 * 60_000,
        }
        phases = tuple(projected)

        for phase_order in permutations(phases):
            with self.subTest(order=phase_order):
                self.store.reset_account_data()
                for index, phase in enumerate(phases):
                    self.store.queue_duration_operation(
                        phase, (index + 2) * 60_000, now_ms=100 + index
                    )
                request = self.store.sync_payload()
                replacements = {}
                for index, phase in enumerate(phase_order):
                    replacements[phase] = self.store.queue_duration_operation(
                        phase, projected[phase], now_ms=200 + index
                    )
                response = self._canonical_response(request, revision=1)
                response["durationsMs"] = canonical

                self.store.apply_sync(response, request)
                first = self.store.load()
                self.assertEqual(first["settings"]["durationsMs"], projected)
                self.assertEqual(
                    {item["phase"]: item for item in first["pendingDurations"]},
                    replacements,
                )

                with self.assertRaisesRegex(
                    ValueError, "active normal sync claim"
                ):
                    self.store.apply_sync(response, request)
                self.assertEqual(self.store.load(), first)

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
                "selectedTaskOperations",
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
                "selectedTaskAcknowledgements": [],
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
                "selectedTaskId": None,
                "serverTime": utc_timestamp(server_hlc_wall_ms),
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
            "start",
            None,
            "focus",
            settings["durationsMs"],
            now_ms=server_hlc_wall_ms,
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
                            "serverTime": utc_timestamp(1_000),
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

    def test_legacy_selected_task_migration_queues_choice_once(self) -> None:
        task = task_from_title("Legacy focus task")
        settings = self.store.load()["settings"]
        settings["selectedTaskId"] = task["id"]
        self.store.set_meta("settings", settings)
        self.store.set_meta("selectedTaskMigrationComplete", False)
        self.store.close()

        self.store = Store(self.path)
        loaded = self.store.load()
        self.assertEqual(loaded["settings"]["selectedTaskId"], task["id"])
        self.assertEqual(len(loaded["pendingSelectedTasks"]), 1)
        operation = loaded["pendingSelectedTasks"][0]
        self.assertEqual(operation["taskId"], task["id"])
        self.assertEqual(
            (operation["occurredAt"], operation["hlcWallMs"], operation["hlcCounter"]),
            ("1970-01-01T00:00:00.000Z", 0, 0),
        )

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.load()["pendingSelectedTasks"], [operation])

    def test_legacy_selected_task_migration_waits_for_pending_resolution(
        self,
    ) -> None:
        task = task_from_title("Deferred legacy focus task")
        self.store.prepare_resolution({"id": "user-1"}, 1, "merge")
        settings = self.store.load()["settings"]
        settings["selectedTaskId"] = task["id"]
        self.store.set_meta("settings", settings)
        self.store.set_meta("selectedTaskMigrationComplete", False)
        self.store.close()

        self.store = Store(self.path)
        self.assertFalse(self.store.get_meta("selectedTaskMigrationComplete"))
        self.assertEqual(self.store.load()["pendingSelectedTasks"], [])

        self.store.clear_pending_resolution()
        self.store.close()
        self.store = Store(self.path)
        loaded = self.store.load()
        self.assertTrue(self.store.get_meta("selectedTaskMigrationComplete"))
        self.assertEqual(len(loaded["pendingSelectedTasks"]), 1)
        self.assertEqual(loaded["pendingSelectedTasks"][0]["taskId"], task["id"])

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
        self.assertEqual(
            explicit["autoStartOperations"],
            self._wire_preference_operations([explicit_false]),
        )

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
        self.assertEqual(
            first["autoStartOperations"],
            self._wire_preference_operations(operations[:256]),
        )
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
        self.assertEqual(
            second["autoStartOperations"],
            self._wire_preference_operations([operations[256]]),
        )
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
        self.assertEqual(
            request["autoStartOperations"],
            self._wire_preference_operations([sent]),
        )
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
        legacy_request.pop("selectedTaskOperations")
        legacy_queue_ids = dict(pending["queueIds"])
        legacy_queue_ids.pop("autoStartOperations")
        legacy_queue_ids.pop("selectedTaskOperations")
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
                "occurredAt": utc_timestamp(index),
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
        self.store._set_meta("hlc", {"wallMs": 4_095, "counter": 0})
        self.store.connection.commit()
        request = self.store.prepare_resolution({"id": "user-1"}, 1, "merge")
        self.assertEqual(len(request["autoStartOperations"]), 4_096)

        self.store.clear_pending_resolution()
        self.store.connection.execute(
            "INSERT INTO pending_auto_start_operations(id, payload) VALUES (?, ?)",
            rows[4_096],
        )
        self.store._set_meta("hlc", {"wallMs": 4_096, "counter": 0})
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
        completed_at = str(canonical_local["completedAt"])
        remote_history = [
            self._history_item(f"remote-{index}", completed_at)
            for index in range(3)
        ]
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

    def test_offline_auto_break_start_waits_for_applied_finish(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        request = self.store.sync_payload()
        self.assertNotIn(generated, request["commands"])
        self.assertEqual(
            self.store.provisional_auto_break_timer_ids(), {generated["timerId"]}
        )
        canonical_timer, canonical_history = self._canonical_completion(
            request["commands"]
        )
        response = self._canonical_response(
            request,
            revision=1,
            history=canonical_history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        self.assertEqual(self.store.sync_payload()["commands"], [generated])
        self.assertEqual(self.store.provisional_auto_break_timer_ids(), set())
        timer, _history = rebuild_optimistic(
            self.store.load()["snapshot"]["canonicalTimer"],
            self.store.load()["snapshot"]["history"],
            self.store.load()["pending"],
        )
        self.assertEqual((timer["id"], timer["phase"]), (generated["timerId"], "short_break"))

    def test_offline_callback_after_finish_acceptance_queues_sendable_start(
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
        self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        request = self.store.sync_payload()
        canonical_timer, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer
        self.store.apply_sync(response, request)

        generated = self.store.process_auto_break(
            require_canonical=False, now_ms=3_000
        )[0]

        self.assertEqual(self.store.provisional_auto_break_timer_ids(), set())
        self.assertEqual(self.store.sync_payload()["commands"], [generated])

    def test_disabling_after_provisional_start_does_not_cancel_decided_break(
        self,
    ) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        disabled = self.store.set_auto_start_breaks(False, now_ms=3_500)
        request = self.store.sync_payload()
        self.assertIn(
            self._wire_preference_operation(disabled),
            request["autoStartOperations"],
        )
        canonical_timer, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=False,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        resent = self.store.sync_payload()["commands"]
        self.assertEqual(len(resent), 1)
        self.assertEqual(
            self._operation_intent(resent[0]),
            self._operation_intent(generated),
        )
        self.assertGreater(
            (resent[0]["hlcWallMs"], resent[0]["hlcCounter"]),
            (response["serverHlcWallMs"], response["serverHlcCounter"]),
        )
        self.assertFalse(self.store.load()["settings"]["autoStartBreaks"])

    def test_duplicate_second_instance_finish_does_not_supersede_decided_break(
        self,
    ) -> None:
        start, _finish, generated = self._queue_offline_auto_break()
        source_running, _history = rebuild_optimistic(None, [], [start])
        other = Store(self.path)
        try:
            duplicate = other.queue_command(
                "finish",
                source_running,
                "focus",
                self.store.load()["settings"]["durationsMs"],
                now_ms=4_000,
            )
        finally:
            other.close()
        request = self.store.sync_payload()
        self.assertIn(duplicate, request["commands"])
        canonical_timer, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        resent = self.store.sync_payload()["commands"]
        self.assertEqual(len(resent), 1)
        self.assertEqual(
            self._operation_intent(resent[0]),
            self._operation_intent(generated),
        )
        self.assertGreater(
            (resent[0]["hlcWallMs"], resent[0]["hlcCounter"]),
            (response["serverHlcWallMs"], response["serverHlcCounter"]),
        )

    def test_canonical_phase_transforms_dependent_finish_pause_and_resume(self) -> None:
        cases = {
            "completed": ["finish"],
            "paused": ["pause"],
            "running": ["pause", "resume"],
        }
        for expected_status, actions in cases.items():
            with self.subTest(expected_status=expected_status):
                self.store.reset_account_data()
                _start, _finish, generated = self._queue_offline_auto_break()
                timer, _history = rebuild_optimistic(
                    None, [], self.store.load()["pending"]
                )
                dependent = []
                for index, action in enumerate(actions):
                    command = self.store.queue_command(
                        action,
                        timer,
                        "short_break",
                        self.store.load()["settings"]["durationsMs"],
                        now_ms=4_000 + index,
                    )
                    dependent.append(command)
                    timer, _history = rebuild_optimistic(
                        None, [], self.store.load()["pending"]
                    )
                request = self.store.sync_payload()
                canonical_timer, local_history = self._canonical_completion(
                    request["commands"]
                )
                completed_at = str(local_history[0]["completedAt"])
                history = [
                    local_history[0],
                    self._history_item("remote-1", completed_at),
                    self._history_item("remote-2", completed_at),
                    self._history_item("remote-3", completed_at),
                ]
                response = self._canonical_response(
                    request,
                    history=history,
                    auto_start_breaks=True,
                )
                response["canonicalTimer"] = canonical_timer
                response["durationsMs"]["long_break"] = 20 * 60_000

                self.store.apply_sync(response, request)

                released = self.store.sync_payload()["commands"]
                self.assertEqual(
                    [command["id"] for command in released],
                    [generated["id"], *[command["id"] for command in dependent]],
                )
                expected_phase = (
                    "short_break"
                    if expected_status == "completed"
                    else "long_break"
                )
                expected_duration_ms = (
                    5 * 60_000
                    if expected_status == "completed"
                    else 20 * 60_000
                )
                self.assertTrue(
                    all(command["phase"] == expected_phase for command in released)
                )
                self.assertTrue(
                    all(
                        command["plannedDurationMs"] == expected_duration_ms
                        for command in released
                    )
                )
                projected, projected_history = rebuild_optimistic(
                    self.store.load()["snapshot"]["canonicalTimer"],
                    self.store.load()["snapshot"]["history"],
                    self.store.load()["pending"],
                )
                self.assertEqual(projected["phase"], expected_phase)
                self.assertEqual(projected["status"], expected_status)
                if expected_status == "completed":
                    self.assertEqual(projected_history[0]["phase"], expected_phase)
                    self.assertEqual(
                        projected_history[0]["plannedDurationMs"],
                        expected_duration_ms,
                    )

    def test_provisional_chain_covers_every_outcome_phase_restart_and_response_loss(
        self,
    ) -> None:
        chains = (
            ("start",),
            ("start", "pause"),
            ("start", "pause", "resume"),
            ("start", "finish"),
            ("start", "cancel"),
            ("start", "finish", "clear"),
        )
        for actions in chains:
            for outcome in ("applied", "ignored", "rejected"):
                for corrects_to_long in (False, True):
                    for restarts_before_http in (False, True):
                        for loses_response in (False, True):
                            label = (
                                actions,
                                outcome,
                                corrects_to_long,
                                restarts_before_http,
                                loses_response,
                            )
                            with self.subTest(case=label):
                                self.store.reset_account_data()
                                _start, finish, generated = (
                                    self._queue_offline_auto_break()
                                )
                                timer, _history = rebuild_optimistic(
                                    None, [], self.store.load()["pending"]
                                )
                                dependent = [generated]
                                for index, action in enumerate(actions[1:]):
                                    command = self.store.queue_command(
                                        action,
                                        timer,
                                        "short_break",
                                        self.store.load()["settings"]["durationsMs"],
                                        now_ms=4_000 + index,
                                    )
                                    dependent.append(command)
                                    timer, _history = rebuild_optimistic(
                                        None, [], self.store.load()["pending"]
                                    )
                                if restarts_before_http:
                                    self.store.close()
                                    self.store = Store(self.path)
                                request = self.store.sync_payload()
                                self.assertNotIn(generated, request["commands"])
                                canonical_timer, local_history = (
                                    self._canonical_completion(request["commands"])
                                )
                                history = list(local_history)
                                if corrects_to_long:
                                    completed_at = str(
                                        local_history[0]["completedAt"]
                                    )
                                    history.extend(
                                        self._history_item(
                                            f"matrix-remote-{index}", completed_at
                                        )
                                        for index in range(1, 4)
                                    )
                                response = self._canonical_response(
                                    request,
                                    revision=1,
                                    history=history,
                                    auto_start_breaks=True,
                                )
                                response["canonicalTimer"] = canonical_timer
                                response["acknowledgements"] = [
                                    {
                                        "commandId": command["id"],
                                        "outcome": (
                                            outcome
                                            if command["id"] == finish["id"]
                                            else "applied"
                                        ),
                                        "reason": (
                                            "matrix outcome"
                                            if command["id"] == finish["id"]
                                            and outcome != "applied"
                                            else ""
                                        ),
                                    }
                                    for command in request["commands"]
                                ]
                                if loses_response:
                                    self.store.close()
                                    self.store = Store(self.path)
                                    self.assertEqual(
                                        self.store.sync_payload(), request
                                    )
                                self.store.apply_sync(response, request)
                                self.store.close()
                                self.store = Store(self.path)
                                released = [
                                    command
                                    for command in self.store.load()["pending"]
                                    if command["timerId"] == generated["timerId"]
                                ]
                                if outcome == "rejected":
                                    self.assertEqual(released, [])
                                    continue
                                self.assertEqual(
                                    [command["id"] for command in released],
                                    [command["id"] for command in dependent],
                                )
                                expected_phase = (
                                    "long_break"
                                    if corrects_to_long
                                    and "finish" not in actions
                                    else "short_break"
                                )
                                self.assertTrue(
                                    all(
                                        command["phase"] == expected_phase
                                        and command["plannedDurationMs"]
                                        == response["durationsMs"][expected_phase]
                                        for command in released
                                    )
                                )

    def test_canonical_phase_transform_preserves_cancel_intent(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        provisional, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        cancel = self.store.queue_command(
            "cancel",
            provisional,
            "short_break",
            self.store.load()["settings"]["durationsMs"],
            now_ms=4_000,
        )
        request = self.store.sync_payload()
        canonical_timer, local_history = self._canonical_completion(request["commands"])
        completed_at = str(local_history[0]["completedAt"])
        history = [
            local_history[0],
            self._history_item("remote-1", completed_at),
            self._history_item("remote-2", completed_at),
            self._history_item("remote-3", completed_at),
        ]
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        released = self.store.sync_payload()["commands"]
        self.assertEqual(
            [command["id"] for command in released],
            [generated["id"], cancel["id"]],
        )
        self.assertEqual(released[1]["phase"], "long_break")
        projected, _history = rebuild_optimistic(
            self.store.load()["snapshot"]["canonicalTimer"],
            self.store.load()["snapshot"]["history"],
            self.store.load()["pending"],
        )
        self.assertEqual((projected["phase"], projected["status"]), ("long_break", "cancelled"))

    def test_malformed_dependent_chain_drops_invalid_suffix_deterministically(
        self,
    ) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        provisional, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        pause = self.store.queue_command(
            "pause",
            provisional,
            "short_break",
            self.store.load()["settings"]["durationsMs"],
            now_ms=4_000,
        )
        paused, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        resume = self.store.queue_command(
            "resume",
            paused,
            "short_break",
            self.store.load()["settings"]["durationsMs"],
            now_ms=4_001,
        )
        corrupt = dict(pause)
        corrupt["type"] = "unknown"
        self.store.connection.execute(
            "UPDATE pending_commands SET payload = ? WHERE id = ?",
            (json.dumps(corrupt, separators=(",", ":")), pause["id"]),
        )
        self.store.connection.commit()
        before = self.store.load()
        with self.assertRaisesRegex(ValueError, "Pending timer command is invalid"):
            self.store.sync_payload()
        self.assertEqual(self.store.load(), before)
        self.assertIn(generated, before["pending"])
        self.assertIn(resume, before["pending"])

    def test_canonical_phase_correction_preserves_later_focus_selection(self) -> None:
        _start, _finish, _generated = self._queue_offline_auto_break()
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "short_break")
        self.store.set_selected_phase("focus")
        request = self.store.sync_payload()
        canonical_timer, local_history = self._canonical_completion(request["commands"])
        completed_at = str(local_history[0]["completedAt"])
        history = [
            local_history[0],
            self._history_item("remote-1", completed_at),
            self._history_item("remote-2", completed_at),
            self._history_item("remote-3", completed_at),
        ]
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")

    def test_canonical_phase_correction_preserves_later_same_phase_selection(
        self,
    ) -> None:
        _start, _finish, _generated = self._queue_offline_auto_break()
        self.store.set_selected_phase("short_break")
        request = self.store.sync_payload()
        canonical_timer, local_history = self._canonical_completion(request["commands"])
        completed_at = str(local_history[0]["completedAt"])
        response = self._canonical_response(
            request,
            history=[
                local_history[0],
                self._history_item("remote-1", completed_at),
                self._history_item("remote-2", completed_at),
                self._history_item("remote-3", completed_at),
            ],
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )

    def test_ui_projection_load_keeps_pending_timer_and_marker_in_one_snapshot(
        self,
    ) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        loaded_state = Event()
        continue_read = Event()
        results = Queue()

        class PausingStore(Store):
            def provisional_auto_break_timer_ids(self) -> set[str]:
                loaded_state.set()
                if not continue_read.wait(2):
                    raise TimeoutError("writer did not release snapshot read")
                return super().provisional_auto_break_timer_ids()

        def read_projection() -> None:
            store = PausingStore(self.path)
            try:
                results.put(store.load_with_provisional_auto_breaks())
            finally:
                store.close()

        thread = Thread(target=read_projection)
        thread.start()
        self.assertTrue(loaded_state.wait(2))
        self.store.connection.execute("DELETE FROM pending_auto_break_starts")
        self.store.connection.commit()
        continue_read.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())

        state, timer_ids = results.get_nowait()
        self.assertIn(generated, state["pending"])
        self.assertEqual(timer_ids, {generated["timerId"]})

    def test_ignored_and_rejected_finish_drop_offline_auto_break_start(self) -> None:
        for outcome in ("ignored", "rejected"):
            with self.subTest(outcome=outcome):
                self.store.reset_account_data()
                start, finish, generated = self._queue_offline_auto_break()
                request = self.store.sync_payload()
                running, _history = rebuild_optimistic(None, [], [start])
                response = self._canonical_response(
                    request,
                    revision=1,
                    auto_start_breaks=True,
                )
                response["canonicalTimer"] = running
                response["acknowledgements"] = [
                    {
                        "commandId": item["id"],
                        "outcome": outcome if item["id"] == finish["id"] else "applied",
                        "reason": "finish not accepted" if item["id"] == finish["id"] else "",
                    }
                    for item in request["commands"]
                ]

                notices = self.store.apply_sync(response, request)

                self.assertEqual(notices, ["finish not accepted"])
                self.assertNotIn(generated, self.store.load()["pending"])
                self.assertEqual(self.store.sync_payload()["commands"], [])
                self.assertEqual(self.store.provisional_auto_break_timer_ids(), set())
                self.assertEqual(
                    self.store.load()["snapshot"]["canonicalTimer"], running
                )
                self.assertEqual(
                    self.store.load()["settings"]["selectedPhase"], "focus"
                )

    def test_non_applied_finish_rolls_back_only_automatic_phase_advance(
        self,
    ) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "short_break")
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response["canonicalTimer"] = running
        response["acknowledgements"] = [
            {
                "commandId": command["id"],
                "outcome": "rejected" if command["id"] == finish["id"] else "applied",
                "reason": "finish rejected" if command["id"] == finish["id"] else "",
            }
            for command in request["commands"]
        ]

        self.store.apply_sync(response, request)

        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM pending_phase_advances"
            ).fetchone()
        )

        self.store.reset_account_data()
        self.store.set_selected_phase("short_break")
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "short_break", settings["durationsMs"], now_ms=3_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        finish = self.store.queue_command(
            "finish", running, "short_break", settings["durationsMs"], now_ms=4_000
        )
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response["canonicalTimer"] = running
        response["acknowledgements"] = [
            {
                "commandId": command["id"],
                "outcome": "ignored" if command["id"] == finish["id"] else "applied",
                "reason": "finish ignored" if command["id"] == finish["id"] else "",
            }
            for command in request["commands"]
        ]

        self.store.apply_sync(response, request)

        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"],
            "short_break",
        )

    def test_finish_phase_rollback_preserves_explicit_choice_and_exact_completion(
        self,
    ) -> None:
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=1_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=2_000
        )
        self.store.set_selected_phase("long_break")
        request = self.store.sync_payload()
        response = self._canonical_response(request, revision=1)
        response["canonicalTimer"] = running
        response["acknowledgements"] = [
            {
                "commandId": command["id"],
                "outcome": "rejected" if command["id"] == finish["id"] else "applied",
                "reason": "finish rejected" if command["id"] == finish["id"] else "",
            }
            for command in request["commands"]
        ]

        self.store.apply_sync(response, request)

        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "long_break")

        self.store.reset_account_data()
        self.store.set_selected_phase("focus")
        settings = self.store.load()["settings"]
        start = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=3_000
        )
        running, _history = rebuild_optimistic(None, [], [start])
        finish = self.store.queue_command(
            "finish", running, "focus", settings["durationsMs"], now_ms=4_000
        )
        request = self.store.sync_payload()
        completed, history = rebuild_optimistic(None, [], request["commands"])
        for item in history:
            item.pop("pending", None)
        response = self._canonical_response(request, revision=1, history=history)
        response["canonicalTimer"] = completed
        response["acknowledgements"] = [
            {
                "commandId": command["id"],
                "outcome": "ignored" if command["id"] == finish["id"] else "applied",
                "reason": "duplicate" if command["id"] == finish["id"] else "",
            }
            for command in request["commands"]
        ]

        self.store.apply_sync(response, request)

        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "short_break")

    def test_superseded_finish_drops_break_and_restores_source_selection(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        request = self.store.sync_payload()
        _completed, history = self._canonical_completion(request["commands"])
        remote_timer = {
            "id": "remote-focus",
            "phase": "focus",
            "status": "running",
            "plannedDurationMs": 25 * 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": "1970-01-01T00:00:04.000Z",
            "taskId": None,
        }
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = remote_timer

        self.store.apply_sync(response, request)

        self.assertNotIn(generated, self.store.load()["pending"])
        self.assertEqual(self.store.load()["settings"]["selectedPhase"], "focus")

    def test_drop_preserves_later_explicit_same_phase_selection(self) -> None:
        start, finish, generated = self._queue_offline_auto_break()
        self.store.set_selected_phase("short_break")
        request = self.store.sync_payload()
        running, _history = rebuild_optimistic(None, [], [start])
        response = self._canonical_response(
            request,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = running
        response["acknowledgements"] = [
            {
                "commandId": command["id"],
                "outcome": "rejected" if command["id"] == finish["id"] else "applied",
                "reason": "superseded" if command["id"] == finish["id"] else "",
            }
            for command in request["commands"]
        ]

        self.store.apply_sync(response, request)

        self.assertNotIn(generated, self.store.load()["pending"])
        self.assertEqual(
            self.store.load()["settings"]["selectedPhase"], "short_break"
        )

    def test_lost_finish_response_preserves_withheld_start_across_restart(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        request = self.store.sync_payload()
        before_timer, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.store.close()

        self.store = Store(self.path)
        self.assertEqual(self.store.sync_payload(), request)
        self.assertEqual(
            self.store.provisional_auto_break_timer_ids(), {generated["timerId"]}
        )
        after_timer, _history = rebuild_optimistic(
            None, [], self.store.load()["pending"]
        )
        self.assertEqual(after_timer, before_timer)

        canonical_timer, canonical_history = self._canonical_completion(
            request["commands"]
        )
        response = self._canonical_response(
            request,
            history=canonical_history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer
        self.store.apply_sync(response, request)
        self.assertEqual(self.store.sync_payload()["commands"], [generated])

    def test_canonical_fourth_focus_reconciles_provisional_short_to_long(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        self.assertEqual(generated["phase"], "short_break")
        request = self.store.sync_payload()
        canonical_timer, local_history = self._canonical_completion(
            request["commands"]
        )
        completed_at = str(local_history[0]["completedAt"])
        history = [
            local_history[0],
            self._history_item("remote-1", completed_at),
            self._history_item("remote-2", completed_at),
            self._history_item("remote-3", completed_at),
        ]
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer
        response["durationsMs"]["long_break"] = 20 * 60_000

        self.store.apply_sync(response, request)

        reconciled = self.store.sync_payload()["commands"]
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["id"], generated["id"])
        self.assertEqual(reconciled[0]["phase"], "long_break")
        self.assertEqual(reconciled[0]["plannedDurationMs"], 20 * 60_000)
        timer, _history = rebuild_optimistic(
            self.store.load()["snapshot"]["canonicalTimer"],
            self.store.load()["snapshot"]["history"],
            self.store.load()["pending"],
        )
        self.assertEqual((timer["phase"], timer["status"]), ("long_break", "running"))

    def test_remote_timer_supersession_drops_withheld_auto_break_start(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        request = self.store.sync_payload()
        _completed, history = self._canonical_completion(request["commands"])
        remote_timer = {
            "id": "newer-remote-timer",
            "phase": "short_break",
            "status": "running",
            "plannedDurationMs": 5 * 60_000,
            "elapsedAtAnchorMs": 0,
            "anchorAt": "1970-01-01T00:00:04.000Z",
            "taskId": None,
        }
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = remote_timer

        self.store.apply_sync(response, request)

        self.assertNotIn(generated, self.store.load()["pending"])
        self.assertEqual(self.store.sync_payload()["commands"], [])
        timer, _history = rebuild_optimistic(
            remote_timer, self.store.load()["snapshot"]["history"], []
        )
        self.assertEqual(timer, remote_timer)

    def test_applied_finish_with_remote_clear_does_not_resurrect_break(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        request = self.store.sync_payload()
        _completed, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = None

        self.store.apply_sync(response, request)

        loaded = self.store.load()
        self.assertNotIn(generated, loaded["pending"])
        self.assertEqual(self.store.sync_payload()["commands"], [])
        self.assertIsNone(loaded["snapshot"]["canonicalTimer"])
        self.assertEqual(loaded["settings"]["selectedPhase"], "focus")
        self.assertEqual(self.store.provisional_auto_break_timer_ids(), set())

    def test_process_auto_break_does_not_resurrect_after_canonical_clear(self) -> None:
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
        request = self.store.sync_payload()
        _completed, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = None
        self.store.apply_sync(response, request)

        self.store.connection.execute(
            "INSERT INTO pending_auto_breaks("
            "finish_command_id, timer_id, finish_device_sequence) VALUES (?, ?, ?)",
            (finish["id"], finish["timerId"], finish["deviceSequence"]),
        )
        self.store.connection.commit()

        self.assertEqual(
            self.store.process_auto_break(require_canonical=False, now_ms=3_000), []
        )
        self.assertEqual(self.store.load()["pending"], [])
        self.assertFalse(self.store.has_pending_auto_break())

    def test_manual_start_remains_sendable_and_supersedes_withheld_auto_break(
        self,
    ) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        settings = self.store.load()["settings"]
        manual = self.store.queue_command(
            "start", None, "focus", settings["durationsMs"], now_ms=4_000
        )
        request = self.store.sync_payload()
        self.assertIn(manual, request["commands"])
        self.assertNotIn(generated, request["commands"])
        canonical_timer, history = self._canonical_completion(request["commands"])
        response = self._canonical_response(
            request,
            history=history,
            auto_start_breaks=True,
        )
        response["canonicalTimer"] = canonical_timer

        self.store.apply_sync(response, request)

        self.assertEqual(canonical_timer["id"], manual["timerId"])
        self.assertNotIn(generated, self.store.load()["pending"])
        self.assertEqual(
            self.store.load()["snapshot"]["canonicalTimer"], canonical_timer
        )

    def test_withheld_auto_break_start_does_not_consume_command_batch_slot(self) -> None:
        _start, _finish, generated = self._queue_offline_auto_break()
        settings = self.store.load()["settings"]
        manual = [
            self.store.queue_command(
                "start", None, "focus", settings["durationsMs"], now_ms=4_000 + index
            )
            for index in range(255)
        ]

        request = self.store.sync_payload()

        self.assertEqual(len(request["commands"]), 256)
        self.assertNotIn(generated, request["commands"])
        self.assertNotIn(manual[-1], request["commands"])

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
        self.store.set_selected_task_id(task["id"], now_ms=101)
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
        response["serverTime"] = utc_timestamp(response["serverHlcWallMs"])
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
        self.assertEqual(
            self.store.sync_payload()["autoStartOperations"],
            self._wire_preference_operations([operation]),
        )

        malformed = self._canonical_response(
            request, revision=1, auto_start_breaks=True
        )
        malformed["autoStartAcknowledgements"] = []
        before = self.store.load()
        with self.assertRaisesRegex(ValueError, "auto-start acknowledgement set"):
            self.store.apply_sync(malformed, request)
        self.assertEqual(self.store.load(), before)

    def test_focus_start_has_task_but_break_start_does_not(self) -> None:
        task = task_from_title("Release")
        self.store.queue_task_operation("upsert", task, now_ms=1_784_548_799_998)
        self.store.set_selected_task_id(task["id"], now_ms=1_784_548_799_999)
        settings = self.store.load()["settings"]
        focus = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            task["id"],
            now_ms=1_784_548_800_000,
        )
        self.assertEqual(focus["taskId"], task["id"])

        request = self.store.sync_payload()
        self.store.apply_sync(
            self._canonical_response(
                request,
                revision=1,
                tasks=[task],
                selected_task_id=task["id"],
            ),
            request,
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

    def test_focus_start_omits_unavailable_selected_task(self) -> None:
        task = task_from_title("Unavailable focus task")
        self.store.queue_task_operation("upsert", task, now_ms=1_000)
        self.store.set_selected_task_id(task["id"], now_ms=2_000)
        self.store.queue_task_operation("delete", task, now_ms=3_000)

        settings = self.store.load()["settings"]
        command = self.store.queue_command(
            "start",
            None,
            "focus",
            settings["durationsMs"],
            task["id"],
            now_ms=4_000,
        )

        self.assertNotIn("taskId", command)
        self.assertEqual(
            self.store.load()["settings"]["selectedTaskId"], task["id"]
        )

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
