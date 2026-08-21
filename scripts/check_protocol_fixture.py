#!/usr/bin/env python3
"""Pin and sanity-check the byte-identical cross-client protocol fixture."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

EXPECTED = "544fa8a8f33361e80421e1f8395223c6a1e1ff243f9583b6baee6d2a1f1112d0"
CANDIDATES = (
    "docs/protocol-fixtures-v1.json",
    "Tests/Fixtures/protocol-fixtures-v1.json",
    "app/src/test/resources/protocol-fixtures-v1.json",
    "tests/fixtures/protocol-fixtures-v1.json",
)
root = Path(__file__).resolve().parents[1]
path = next((root / item for item in CANDIDATES if (root / item).exists()), None)
if path is None:
    raise SystemExit("protocol fixture missing")
raw = path.read_bytes()
got = hashlib.sha256(raw).hexdigest()
if got != EXPECTED:
    raise SystemExit(f"protocol fixture hash {got}, expected {EXPECTED}")
data = json.loads(raw)
request = data["syncRequest"]
bootstrap = data["bootstrapResolutionRequest"]
for envelope in (request, bootstrap):
    for domain in ("commands", "taskOperations", "durationOperations", "autoStartOperations", "selectedTaskOperations"):
        if not envelope.get(domain):
            raise SystemExit(f"protocol fixture missing {domain}")
ack_domains = (
    "acknowledgements", "taskAcknowledgements", "durationAcknowledgements",
    "autoStartAcknowledgements", "selectedTaskAcknowledgements",
)
if any(len(data["syncResponse"].get(domain, [])) != 1 for domain in ack_domains):
    raise SystemExit("protocol fixture acknowledgement coverage is incomplete")
nullable = data["nullableCollectionWireCases"]
if "selectedTaskOperations" in nullable["selectedTaskOperationsOmitted"]:
    raise SystemExit("omitted selected-task collection became present")
if nullable["selectedTaskOperationsPresentEmpty"].get("selectedTaskOperations") != []:
    raise SystemExit("present-empty selected-task collection is not empty")
if data["selectedTaskWireCases"][1]["taskId"] is not None:
    raise SystemExit("deselection must be explicit null")
for record_name in ("irohGenesisRecord", "irohSelectedTaskRecord"):
    record = data[record_name]
    if set(record) != {"domain", "deviceId", "operation"}:
        raise SystemExit(f"bad Iroh wrapper: {record_name}")
if data["irohGenesisRecord"]["operation"].get("selectedTaskId", "missing") is not None:
    raise SystemExit("Iroh genesis must preserve explicit-null selected task")
identifiers = [request["deviceId"], request["commands"][0]["id"], request["commands"][0]["timerId"]]
for domain in ("taskOperations", "durationOperations", "autoStartOperations", "selectedTaskOperations"):
    identifiers.append(request[domain][0]["id"])
identifiers.append(bootstrap["requestId"])
for value in identifiers:
    parsed = uuid.UUID(value)
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise SystemExit(f"identifier is not RFC 9562 UUIDv7: {value}")
clocks = [request[name][0] for name in ("commands", "taskOperations", "durationOperations", "autoStartOperations", "selectedTaskOperations")]
if [(item["hlcWallMs"], item["hlcCounter"]) for item in clocks] != [(1787270400000, counter) for counter in range(5)]:
    raise SystemExit("fixture HLC sequence is not canonical")
for case in data["timeZoneCases"]:
    left, right = (case.get("before") or case["first"]), (case.get("after") or case["second"])
    elapsed = int((datetime.fromisoformat(right) - datetime.fromisoformat(left)).total_seconds() * 1000)
    if elapsed != case["elapsedMs"]:
        raise SystemExit(f"bad timezone case {case['name']}")
print(f"protocol fixture ok ({got})")
