from __future__ import annotations

import ast
import inspect
import tempfile
import textwrap
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from pomodorough.core import task_from_title
from pomodorough.storage import Store, utc_timestamp
from pomodorough.storage_canonical_acknowledgements import (
    CanonicalAcknowledgementStorage,
)
from pomodorough.storage_canonical_installation import AtomicCanonicalInstaller
from pomodorough.storage_canonical_reconciliation import (
    SharedCoreReconciliationAdapter,
)
from pomodorough.storage_canonical_validation import CanonicalWireValidator


def _empty_request() -> dict[str, object]:
    return {
        "deviceId": "device-a",
        "lastRevision": 0,
        "commands": [],
        "taskOperations": [],
        "durationOperations": [],
        "autoStartOperations": [],
        "selectedTaskOperations": [],
    }


def _canonical_response(request: dict[str, object]) -> dict[str, object]:
    acknowledgement_specs = (
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
        for response_key, request_key, id_key in acknowledgement_specs
    }
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
        serverTime=utc_timestamp(1_000),
        serverHlcWallMs=1_000,
        serverHlcCounter=0,
    )
    return response


class CanonicalStorageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_facade_composes_explicit_canonical_responsibilities(self) -> None:
        storage = self.store._canonical_storage

        self.assertIsInstance(storage._validation, CanonicalWireValidator)
        self.assertIsInstance(
            storage._acknowledgements, CanonicalAcknowledgementStorage
        )
        self.assertIsInstance(storage._reconciliation, SharedCoreReconciliationAdapter)
        self.assertIsInstance(storage._installation, AtomicCanonicalInstaller)
        components = (
            storage._validation,
            storage._acknowledgements,
            storage._reconciliation,
            storage._installation,
        )
        for component in components:
            with self.subTest(component=type(component).__name__):
                self.assertIs(component._dependencies, storage._dependencies)
                self.assertIs(component._hooks, storage)
                self.assertFalse(hasattr(component, "_store"))
        self.assertIsNot(storage._dependencies, self.store)
        self.assertEqual(storage._dependencies.device_id, self.store.device_id)
        with self.assertRaises(FrozenInstanceError):
            setattr(storage._dependencies, "device_id", "replacement")

    def test_typecheck_fixture_matches_production_dependency_wiring(self) -> None:
        production = ast.parse(
            textwrap.dedent(inspect.getsource(Store._canonical_dependencies))
        )
        fixture = ast.parse(
            Path(__file__).with_name("typecheck_canonical_dependencies.py").read_text()
        )

        class NormalizeProviderName(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.Name:
                if node.id in {"self", "store"}:
                    return ast.copy_location(ast.Name(id="provider", ctx=node.ctx), node)
                return node

        def returned_constructor(tree: ast.Module) -> ast.expr:
            function = next(
                node for node in tree.body if isinstance(node, ast.FunctionDef)
            )
            returned = next(
                node for node in function.body if isinstance(node, ast.Return)
            )
            assert returned.value is not None
            return NormalizeProviderName().visit(returned.value)

        self.assertEqual(
            ast.dump(returned_constructor(production), include_attributes=False),
            ast.dump(returned_constructor(fixture), include_attributes=False),
        )

    def test_wire_validation_error_order_is_stable(self) -> None:
        request = _empty_request()
        base = _canonical_response(request)
        cases = (
            (
                {"durationsMs": {}, "autoStartBreaks": "invalid"},
                "Server returned invalid duration preferences.",
            ),
            (
                {"autoStartBreaks": "invalid", "revision": True},
                "Server returned an invalid auto-start preference.",
            ),
            (
                {"revision": True, "history": "invalid"},
                "Server returned an invalid revision.",
            ),
            (
                {"history": "invalid", "tasks": "invalid"},
                "Server returned invalid timer history.",
            ),
            (
                {"tasks": "invalid", "selectedTaskId": "missing"},
                "Server returned invalid tasks.",
            ),
            (
                {"selectedTaskId": "missing", "canonicalTimer": {}},
                "Server returned an invalid selected-task preference.",
            ),
            (
                {"canonicalTimer": {}, "serverTime": "invalid"},
                "Server returned an invalid canonical timer.",
            ),
        )
        for updates, message in cases:
            with self.subTest(message=message):
                response = {**deepcopy(base), **updates}
                with self.assertRaises(ValueError) as raised:
                    self.store._validated_sync_response(response, request)
                self.assertEqual(str(raised.exception), message)

    def test_acknowledgement_set_precedes_later_wire_validation(self) -> None:
        request = _empty_request()
        request["commands"] = [{"id": "sent-command"}]
        response = _canonical_response(request)
        response["acknowledgements"] = [
            {"commandId": "other-command", "outcome": "applied", "reason": ""}
        ]
        response["durationsMs"] = {}

        with self.assertRaises(ValueError) as raised:
            self.store._validated_sync_response(response, request)

        self.assertEqual(
            str(raised.exception),
            "Sync returned an invalid command acknowledgement set.",
        )

    def test_active_claim_check_precedes_wire_validation(self) -> None:
        claimed = self.store.sync_payload()
        mismatched = {**claimed, "lastRevision": claimed["lastRevision"] + 1}

        with self.assertRaises(ValueError) as raised:
            self.store.apply_sync({}, mismatched)

        self.assertEqual(
            str(raised.exception),
            "Sync response did not match an active normal sync claim.",
        )
        self.assertEqual(self.store.pending_sync(), claimed)

    def test_partial_install_failure_rolls_back_and_does_not_publish_anchor(
        self,
    ) -> None:
        task = task_from_title("Rollback queue")
        self.store.queue_task_operation("upsert", task, now_ms=500)
        request = self.store.sync_payload()
        response = _canonical_response(request)
        response["tasks"] = [task]
        before = self.store.load()
        before_claim = self.store.pending_sync()
        self.assertTrue(before["pendingTasks"])
        statements: list[str] = []
        storage = self.store._canonical_storage
        install_snapshot = storage._install_snapshot

        def fail_after_snapshot(*args: object, **kwargs: object) -> None:
            install_snapshot(*args, **kwargs)
            raise RuntimeError("injected install failure")

        self.store.connection.set_trace_callback(statements.append)
        try:
            with (
                patch.object(storage, "_install_snapshot", fail_after_snapshot),
                patch.object(
                    self.store,
                    "_clock_sample_for_response",
                    wraps=self.store._clock_sample_for_response,
                ) as clock_sample,
                patch.object(self.store, "_set_trusted_time_anchor") as publish_anchor,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected install failure"):
                    self.store.apply_sync(
                        response,
                        request,
                        request_physical_ms=900,
                        received_physical_ms=1_100,
                        request_monotonic_ms=10_000,
                        received_monotonic_ms=10_200,
                    )
                clock_sample.assert_called_once_with(1_000, 900, 1_100, 10_000, 10_200)
                publish_anchor.assert_not_called()
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertEqual(statements[0], "BEGIN IMMEDIATE")
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertNotIn("COMMIT", statements)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.store.pending_sync(), before_claim)


if __name__ == "__main__":
    unittest.main()
