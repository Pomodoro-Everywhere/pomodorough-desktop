from __future__ import annotations

import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

from pomodorough.core import rebuild_optimistic, task_from_title
from pomodorough.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

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
                "durationAcknowledgements": [],
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
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
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
                "serverHlcWallMs": operation["hlcWallMs"],
                "serverHlcCounter": operation["hlcCounter"],
            },
            payload,
        )
        loaded = self.store.load()
        self.assertEqual(loaded["pendingTasks"], [])
        self.assertEqual(loaded["snapshot"]["tasks"], [task])
        self.assertEqual(loaded["snapshot"]["knownTasks"], [task])

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

    def test_stale_general_settings_save_preserves_new_duration(self) -> None:
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
        self.assertTrue(settings["autoStartBreaks"])

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
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 30 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
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
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 30 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
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
            "revision": 1,
            "canonicalTimer": None,
            "history": [],
            "tasks": [],
            "durationsMs": {
                "focus": 40 * 60_000,
                "short_break": 6 * 60_000,
                "long_break": 16 * 60_000,
            },
            "serverHlcWallMs": 1_000,
            "serverHlcCounter": 0,
        }

        invalid_sets = (
            ("acknowledgements", "command"),
            ("taskAcknowledgements", "task"),
            ("durationAcknowledgements", "duration"),
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
            },
        )
        server_hlc_wall_ms = int(time.time() * 1000) + 60_000
        self.store.apply_sync(
            {
                "acknowledgements": [],
                "taskAcknowledgements": [],
                "durationAcknowledgements": [],
                "revision": 1,
                "canonicalTimer": None,
                "history": [],
                "tasks": [],
                "durationsMs": {
                    "focus": 2 * 60_000,
                    "short_break": 60_000,
                    "long_break": 10_800_000,
                },
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
                            "revision": 1,
                            "canonicalTimer": None,
                            "history": [],
                            "tasks": [],
                            "durationsMs": {
                                "focus": duration_ms,
                                "short_break": 5 * 60_000,
                                "long_break": 15 * 60_000,
                            },
                            "serverHlcWallMs": 1_000,
                            "serverHlcCounter": 0,
                        },
                        request,
                    )

        self.assertEqual(self.store.load()["snapshot"]["revision"], 0)

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
                "durationsMs": {
                    "focus": 25 * 60_000,
                    "short_break": 5 * 60_000,
                    "long_break": 15 * 60_000,
                },
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
        self.assertTrue(loaded["settings"]["autoStartBreaks"])

        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.load()["pendingDurations"], [])


if __name__ == "__main__":
    unittest.main()
