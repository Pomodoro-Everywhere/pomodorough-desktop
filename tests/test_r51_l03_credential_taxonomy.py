"""R51-L03: credential taxonomy survives scrubbed SDK envelopes.

Every finite operation/cause pair stays distinguishable in tags and
context after production `before_send` scrubbing. The fixed exception
`value` stays `[Filtered]`; arbitrary nested `key`/`value` dicts stay
filtered; no original exception chain or secret canary survives.
Uses a real SDK client with in-memory transport and dummy DSN.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from pomodorough import sentry_monitoring as monitoring

CANARY = "canary-r51l03-TOKEN-abc123"
OPERATIONS = [
    "save",
    "login",
    "sync",
    "preview_bootstrap",
    "resolve_bootstrap",
    "revision_stream",
    "restore",
]
CAUSES = ["filesystem", "secure-store-infra", "unexpected"]


class MemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture()
def sdk_events() -> Iterator[tuple[sentry_sdk.Client, MemoryTransport]]:
    transport = MemoryTransport()
    client = sentry_sdk.Client(
        dsn="https://public@example.invalid/1",
        transport=transport,
        default_integrations=False,
        include_local_variables=False,
        send_client_reports=False,
        before_send=monitoring.scrub_sentry_event,
    )
    try:
        yield client, transport
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _budget() -> Iterator[None]:
    monitoring.reset_cloud_credential_budget()
    try:
        yield
    finally:
        monitoring.reset_cloud_credential_budget()


def _capture_all(sdk_events) -> list[dict]:
    client, transport = sdk_events
    with (
        patch.dict(os.environ, {"POMODOROUGH_SENTRY_ALLOW_IN_TESTS": "1"}),
        sentry_sdk.isolation_scope() as scope,
    ):
        scope.set_client(client)
        for operation in OPERATIONS:
            for cause in CAUSES:
                monitoring.reset_cloud_credential_budget()
                assert monitoring.capture_cloud_credential(operation, cause) is True
    return transport.events


def test_all_pairs_distinguishable_post_sdk(sdk_events) -> None:
    events = _capture_all(sdk_events)
    assert len(events) == len(OPERATIONS) * len(CAUSES)
    seen: set[tuple[str, str]] = set()
    for event in events:
        tags = event.get("tags") or {}
        operation = tags.get("cloud_credential.operation")
        cause = tags.get("cloud_credential.cause")
        seen.add((operation, cause))
        contexts = event.get("contexts") or {}
        inner = contexts.get("cloud_credential") or {}
        assert inner == {"operation": operation, "cause": cause}
        values = (event.get("exception") or {}).get("values") or []
        assert len(values) == 1 and values[0].get("type") == "RuntimeError"
        assert values[0].get("value") == "[Filtered]"
        assert not values[0].get("stacktrace", {}).get("frames")
    assert seen == {(op, ca) for op in OPERATIONS for ca in CAUSES}


def test_no_canary_or_original_chain(sdk_events) -> None:
    events = _capture_all(sdk_events)
    rendered = json.dumps(events)
    assert CANARY not in rendered
    for raw in ("PermissionError", "SecureStoreError", "Traceback"):
        assert raw not in rendered
    for event in events:
        values = (event.get("exception") or {}).get("values") or []
        assert len(values) == 1


def test_nested_key_value_stays_filtered() -> None:
    event = {
        "tags": {
            "cloud_credential.operation": "sync",
            "cloud_credential.cause": CANARY,
        },
        "contexts": {"nested": {"key": "k", "value": CANARY}},
        "extra": {"nested": {"key": "k", "value": CANARY}},
        "exception": {"values": [{"type": "RuntimeError", "value": CANARY}]},
    }
    scrubbed = monitoring.scrub_sentry_event(json.loads(json.dumps(event)), None)
    rendered = json.dumps(scrubbed)
    assert CANARY not in rendered
    assert scrubbed["tags"]["cloud_credential.operation"] == "[Filtered]"
    assert scrubbed["tags"]["cloud_credential.cause"] == "[Filtered]"
    assert "cloud_credential" not in (scrubbed.get("contexts") or {})
    assert scrubbed["extra"]["nested"] == {"key": "[Filtered]", "value": "[Filtered]"}


def test_message_only_envelope_gains_taxonomy() -> None:
    event = {
        "exception": {
            "values": [{"type": "RuntimeError", "value": "cloud credential restore failed: unexpected"}]
        }
    }
    scrubbed = monitoring.scrub_sentry_event(dict(event), None)
    assert scrubbed["tags"]["cloud_credential.operation"] == "restore"
    assert scrubbed["tags"]["cloud_credential.cause"] == "unexpected"
    assert scrubbed["contexts"]["cloud_credential"] == {
        "operation": "restore",
        "cause": "unexpected",
    }
    values = scrubbed["exception"]["values"]
    assert values[0]["value"] == "[Filtered]"
