"""v2-capable SharedCore double for desktop contract tests.

The pinned production bundle predates ``reconcile.rebase.v2`` (Core 0.37.0
answers it with "unsupported shared-core operation"), while the accepted
immutable contract requires clients to call v2 and never fall back to v1.
Production code therefore calls v2 unconditionally and surfaces recovery
when the Core cannot serve it.

Tests must still verify the client glue (durable never-sent proof, exact
payload persistence, atomic snapshot/head install, safe optimistic
projection). This double delegates the authoritative decisions to the real
packaged Core and emulates only the v2 delivery mechanics per
pomodorough-core/IMMUTABLE_RECONCILIATION.md:

* neverSent shape/identity validation with Core's error vocabulary,
* timer dependency resolution, acknowledgement filtering, and
  generated-break normalization via the real ``reconcile.rebase.v1``
  (the v1/v2 code paths share that stage; v2 only adds the drop guard),
* v2 drop guard: no possibly-delivered dependent may be discarded,
* immutable clocks: restored ``occurredAt``/``hlcWallMs``/``hlcCounter``
  from the local originals (v2 never rebases, unlike v1),
* projectionPending full-or-empty per-domain gating on never-sent proof
  plus strict ``(hlcWallMs, hlcCounter) > head`` ordering,
* safe projection replayed through the real ``projection.apply.v2``.

It is NOT an authoritative reimplementation: drops, promotions,
normalization, and safe projections all come from Core. Cases needing
immutable timer-order audits beyond what v1 checks must not use it.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pomodorough.shared_core import SharedCore, SharedCoreOperationError

QUEUE_DOMAINS = (
    "commands",
    "taskOperations",
    "durationOperations",
    "autoStartOperations",
    "selectedTaskOperations",
)

_OUTPUT_KEYS = {
    "commands": "pending",
    "taskOperations": "pendingTaskOperations",
    "durationOperations": "pendingDurationOperations",
    "autoStartOperations": "pendingAutoStartOperations",
    "selectedTaskOperations": "pendingSelectedTaskOperations",
}


def _operation_error(detail: str) -> SharedCoreOperationError:
    return SharedCoreOperationError("reconcile.rebase.v2", detail)


class V2EmulatingSharedCore:
    """Real Core plus a spec-derived v2 delivery stage for client tests."""

    TEST_ONLY_DOUBLE = True

    def __init__(self) -> None:
        self.delegate = SharedCore()
        self.calls: list[tuple[str, object, object]] = []

    def dispatch(self, operation: str, input_value: object) -> object:
        if operation == "reconcile.rebase.v2":
            result = self._rebase_v2(input_value)
            self.calls.append((operation, deepcopy(input_value), deepcopy(result)))
            return result
        result = self.delegate.dispatch(operation, input_value)
        self.calls.append((operation, deepcopy(input_value), deepcopy(result)))
        return result

    def _rebase_v2(self, input_value: object) -> dict[str, Any]:
        if not isinstance(input_value, dict):
            raise _operation_error("invalid shared-core input: missing local queues")
        local = input_value.get("local", {})
        sent = input_value.get("sent", {})
        response = input_value.get("response", {})
        dependencies = input_value.get("timerDependencies", [])
        never_sent = self._validated_never_sent(
            input_value.get("neverSent"), local, sent
        )
        try:
            resolved = self.delegate.dispatch(
                "reconcile.rebase.v1",
                {
                    "local": local,
                    "sent": sent,
                    "response": response,
                    "timerDependencies": dependencies,
                },
            )
        except SharedCoreOperationError as error:
            raise _operation_error(error.detail) from error
        assert isinstance(resolved, dict)
        output = deepcopy(resolved)
        self._restore_immutable_clocks(output, local)
        self._enforce_drop_guard(output, local, never_sent)
        head = (response["serverHlcWallMs"], response["serverHlcCounter"])
        pending = {
            domain: output[output_key] for domain, output_key in _OUTPUT_KEYS.items()
        }
        safe = self._projection_pending(pending, never_sent, head)
        projection = self._safe_projection(response, safe)
        output["projectionPending"] = deepcopy(safe)
        output["timer"] = projection["canonicalTimer"]
        output["history"] = projection["history"]
        output["tasks"] = projection["tasks"]
        output["durationsMs"] = projection["durationsMs"]
        output["autoStartBreaks"] = projection["autoStartBreaks"]
        output["selectedTaskId"] = projection["selectedTaskId"]
        return output

    def _validated_never_sent(
        self, claim: object, local: dict[str, Any], sent: dict[str, Any]
    ) -> dict[str, set[str]]:
        if claim is None:
            return {}
        if not isinstance(claim, dict):
            raise _operation_error("invalid neverSent queue")
        proof: dict[str, set[str]] = {}
        for domain, ids in claim.items():
            if domain not in QUEUE_DOMAINS:
                raise _operation_error("unknown neverSent queue")
            if not isinstance(ids, list):
                raise _operation_error("invalid neverSent queue")
            local_ids = {
                item["id"] for item in local.get(domain, []) if isinstance(item, dict)
            }
            sent_ids = {
                item["id"] for item in sent.get(domain, []) if isinstance(item, dict)
            }
            seen: set[str] = set()
            for operation_id in ids:
                if not isinstance(operation_id, str):
                    raise _operation_error("neverSent requires string IDs")
                if (
                    operation_id in seen
                    or operation_id not in local_ids
                    or operation_id in sent_ids
                ):
                    raise _operation_error(
                        "invalid neverSent identity or delivery claim"
                    )
                seen.add(operation_id)
            if seen:
                proof[domain] = seen
        return proof

    @staticmethod
    def _restore_immutable_clocks(
        output: dict[str, Any], local: dict[str, Any]
    ) -> None:
        """Undo v1 clock rebasing; v2 retains every original timestamp."""
        originals = {
            domain: {item["id"]: item for item in local.get(domain, [])}
            for domain in QUEUE_DOMAINS
        }
        for domain, output_key in _OUTPUT_KEYS.items():
            for operation in output[output_key]:
                original = originals[domain][operation["id"]]
                operation["occurredAt"] = original["occurredAt"]
                operation["hlcWallMs"] = original["hlcWallMs"]
                operation["hlcCounter"] = original["hlcCounter"]

    @staticmethod
    def _enforce_drop_guard(
        output: dict[str, Any],
        local: dict[str, Any],
        never_sent: dict[str, set[str]],
    ) -> None:
        frozen = {
            item["id"]
            for item in local.get("commands", [])
            if item["id"] not in never_sent.get("commands", set())
        }
        if set(output["droppedTimerOperationIds"]) & frozen:
            raise _operation_error(
                "reconciliation would discard a possibly delivered dependent"
            )

    @staticmethod
    def _projection_pending(
        pending: dict[str, list[dict[str, Any]]],
        proof: dict[str, set[str]],
        head: tuple[int, int],
    ) -> dict[str, list[dict[str, Any]]]:
        safe = {}
        for domain in QUEUE_DOMAINS:
            queue = pending[domain]
            proven = proof.get(domain, set())
            if queue and all(
                item["id"] in proven
                and (item["hlcWallMs"], item["hlcCounter"]) > head
                for item in queue
            ):
                safe[domain] = queue
            else:
                safe[domain] = []
        return safe

    def _safe_projection(
        self, response: dict[str, Any], safe: dict[str, list[dict[str, Any]]]
    ) -> dict[str, Any]:
        result = self.delegate.dispatch(
            "projection.apply.v2",
            {
                "base": {
                    "canonicalTimer": response["canonicalTimer"],
                    "history": response["history"],
                    "tasks": response["tasks"],
                    "durationsMs": response["durationsMs"],
                    "autoStartBreaks": response["autoStartBreaks"],
                    "selectedTaskId": response["selectedTaskId"],
                },
                "pending": deepcopy(safe),
                "now": response["serverTime"],
            },
        )
        assert isinstance(result, dict)
        return result
