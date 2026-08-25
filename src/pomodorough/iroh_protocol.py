from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .core import task_from_title
from .storage import MAX_SAFE_INTEGER, Store
from .uuid7 import uuid7_parts

PROTOCOL_VERSION = 1
ALPN = b"me.egigoka.pomodorough/sync/1"
INVITE_PREFIX = "pomodorough1."
MAX_FRAME_BODY = 16 * 1024 * 1024
MAX_OPERATION_BODY = 64 * 1024
MAX_ENDPOINT_TICKET = 16 * 1024
MAX_INVENTORY = 1024
MAX_OPERATION_REFS = 255
MAX_PEERS = 64
LEGACY_EPOCH = "1970-01-01T00:00:00.000Z"
DOMAINS = {"genesis", "timer", "task", "duration", "autoStart", "selectedTask"}
ERROR_CODES = {
    "bad_frame",
    "unauthorized",
    "wrong_room",
    "unsupported_version",
    "invalid_request",
    "not_found",
    "immutable_conflict",
    "limit",
    "internal",
}
_ROOM_PREFIX = b"pomodorough-room-v1\0"
_FRAME_PREFIX = b"pomodorough-iroh-frame-v1\0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class IrohProtocolError(ValueError):
    pass


class ImmutableConflict(IrohProtocolError):
    pass


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str, *, label: str = "base64url") -> bytes:
    if not isinstance(value, str) or not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise IrohProtocolError(f"Malformed {label}.")
    remainder = len(value) % 4
    if remainder == 1:
        raise IrohProtocolError(f"Malformed {label}.")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - remainder) % 4))
    except (ValueError, UnicodeEncodeError) as error:
        raise IrohProtocolError(f"Malformed {label}.") from error
    if b64url_encode(decoded) != value:
        raise IrohProtocolError(f"Malformed {label}.")
    return decoded


def room_id_for_secret(secret: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise IrohProtocolError("Room secret must be exactly 32 bytes.")
    return b64url_encode(hashlib.sha256(_ROOM_PREFIX + secret).digest())


def valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def valid_room_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(b64url_decode(value, label="room ID")) == 32
    except IrohProtocolError:
        return False


def valid_request_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid7_parts(str(uuid.UUID(value)))
    except (ValueError, AttributeError):
        return False
    return True


def _ticket_endpoint_id(ticket: str) -> str:
    try:
        import iroh
    except (ImportError, OSError) as error:
        raise IrohProtocolError(
            "Iroh support is unavailable because optional dependency iroh==1.1.0 is not installed for this platform."
        ) from error
    try:
        return str(iroh.EndpointTicket.from_string(ticket).endpoint_addr().id())
    except Exception as error:
        raise IrohProtocolError("Endpoint ticket is malformed.") from error


@dataclass(frozen=True)
class RoomInvite:
    room_id: str
    endpoint_ticket: str
    endpoint_id: str
    room_secret: bytes
    room_name: str | None = None

    def encode(self) -> str:
        if _ticket_endpoint_id(self.endpoint_ticket) != self.endpoint_id:
            raise IrohProtocolError("Endpoint ticket identity changed.")
        return create_invite(
            self.room_secret,
            self.endpoint_ticket,
            self.room_name,
        )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise IrohProtocolError("JSON object contains duplicate fields.")
        value[key] = item
    return value


def _valid_room_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)
    )


def _utf8_length(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def create_invite(
    room_secret: bytes,
    endpoint_ticket: str,
    room_name: str | None = None,
    *,
    ticket_parser: Callable[[str], str] = _ticket_endpoint_id,
) -> str:
    ticket_size = _utf8_length(endpoint_ticket)
    if ticket_size is None or not endpoint_ticket or ticket_size > MAX_ENDPOINT_TICKET:
        raise IrohProtocolError("Endpoint ticket must contain at most 16 KiB.")
    if room_name is not None and not _valid_room_name(room_name):
        raise IrohProtocolError("Room name must contain 1 through 64 Unicode scalar values.")
    endpoint_id = ticket_parser(endpoint_ticket)
    if not endpoint_id:
        raise IrohProtocolError("Endpoint ticket has no endpoint identity.")
    document: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "roomId": room_id_for_secret(room_secret),
        "endpointTicket": endpoint_ticket,
        "roomSecret": b64url_encode(room_secret),
    }
    if room_name is not None:
        document["roomName"] = room_name
    body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return INVITE_PREFIX + b64url_encode(body)


def parse_invite(
    text: str,
    *,
    ticket_parser: Callable[[str], str] = _ticket_endpoint_id,
) -> RoomInvite:
    if not isinstance(text, str) or not text.startswith(INVITE_PREFIX):
        raise IrohProtocolError(f"Invite must start with {INVITE_PREFIX}")
    try:
        document = json.loads(
            b64url_decode(text[len(INVITE_PREFIX) :], label="invite payload").decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IrohProtocolError("Invite payload must be a UTF-8 JSON object.") from error
    required = {"v", "roomId", "endpointTicket", "roomSecret"}
    if not isinstance(document, dict) or not required <= set(document) or not set(document) <= required | {"roomName"}:
        raise IrohProtocolError("Invite has missing or unknown fields.")
    if isinstance(document["v"], bool) or document["v"] != PROTOCOL_VERSION:
        raise IrohProtocolError("Invite protocol version is unsupported.")
    room_id = document["roomId"]
    ticket = document["endpointTicket"]
    room_name = document.get("roomName")
    if not valid_room_id(room_id):
        raise IrohProtocolError("Invite room ID is malformed.")
    ticket_size = _utf8_length(ticket)
    if ticket_size is None or not ticket or ticket_size > MAX_ENDPOINT_TICKET:
        raise IrohProtocolError("Invite endpoint ticket must contain at most 16 KiB.")
    if "roomName" in document and not _valid_room_name(room_name):
        raise IrohProtocolError("Invite room name must contain 1 through 64 Unicode scalar values.")
    secret = b64url_decode(document["roomSecret"], label="room secret")
    if len(secret) != 32:
        raise IrohProtocolError("Invite room secret must be exactly 32 bytes.")
    if room_id_for_secret(secret) != room_id:
        raise IrohProtocolError("Invite room ID does not match its room secret.")
    endpoint_id = ticket_parser(ticket)
    if not endpoint_id:
        raise IrohProtocolError("Invite endpoint ticket has no endpoint identity.")
    return RoomInvite(room_id, ticket, endpoint_id, secret, room_name)


def encode_frame(body: bytes, room_secret: bytes) -> bytes:
    if not isinstance(body, bytes) or len(body) > MAX_FRAME_BODY or len(room_secret) != 32:
        raise IrohProtocolError("Frame is invalid or exceeds 16 MiB.")
    mac = hmac.digest(room_secret, _FRAME_PREFIX + body, "sha256")
    return struct.pack(">I", len(body)) + mac + body


def decode_frame(frame: bytes, room_secret: bytes) -> bytes:
    if not isinstance(frame, bytes) or len(frame) < 36 or len(room_secret) != 32:
        raise IrohProtocolError("Frame is malformed.")
    length = struct.unpack(">I", frame[:4])[0]
    if length > MAX_FRAME_BODY or len(frame) != length + 36:
        raise IrohProtocolError("Frame length is invalid.")
    body = frame[36:]
    expected = hmac.digest(room_secret, _FRAME_PREFIX + body, "sha256")
    if not hmac.compare_digest(frame[4:36], expected):
        raise IrohProtocolError("Frame authentication failed.")
    return body


def _canonical_string(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_SAFE_INTEGER:
            raise IrohProtocolError("Canonical record integer exceeds safe range.")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_string(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(
            _canonical_string(key) + ":" + _canonical_string(value[key]) for key in keys
        ) + "}"
    raise IrohProtocolError("Record contains unsupported canonical JSON values.")


def canonical_json(value: Any) -> bytes:
    try:
        return _canonical_string(value).encode("utf-8")
    except UnicodeEncodeError as error:
        raise IrohProtocolError("Record contains invalid Unicode.") from error


def record_digest(record: dict[str, Any]) -> str:
    validate_record(record)
    return b64url_encode(hashlib.sha256(canonical_json(record)).digest())


def _exact_keys(value: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    if not required <= set(value) or not set(value) <= required | optional:
        raise IrohProtocolError("Object has missing or unknown fields.")


def _valid_clock(operation: dict[str, Any], *, allow_zero: bool = False) -> None:
    try:
        Store._operation_clock(operation, allow_legacy_zero=allow_zero)
    except ValueError as error:
        raise IrohProtocolError("Operation occurrence and logical clock are invalid.") from error


def _valid_peer_clock(operation: dict[str, Any], *, allow_zero: bool = False) -> None:
    from .core import parse_timestamp_ms

    occurred_at = operation.get("occurredAt")
    occurred_ms = parse_timestamp_ms(occurred_at) if isinstance(occurred_at, str) else None
    wall_ms = operation.get("hlcWallMs")
    counter = operation.get("hlcCounter")
    if (
        occurred_ms is None
        or isinstance(wall_ms, bool)
        or not isinstance(wall_ms, int)
        or isinstance(counter, bool)
        or not isinstance(counter, int)
        or not 0 <= wall_ms <= MAX_SAFE_INTEGER
        or not 0 <= counter <= MAX_SAFE_INTEGER
    ):
        raise IrohProtocolError("Operation occurrence and logical clock are invalid.")
    if (wall_ms, counter) == (0, 0):
        if not allow_zero or occurred_at != LEGACY_EPOCH:
            raise IrohProtocolError("Legacy operation sentinel is invalid.")
        return
    if wall_ms == 0 or abs(wall_ms - occurred_ms) > 300_000:
        raise IrohProtocolError("Operation occurrence and logical clock are invalid.")


def _valid_peer_timer(operation: dict[str, Any]) -> None:
    sequence = operation.get("deviceSequence")
    planned = operation.get("plannedDurationMs")
    observed = operation.get("observedElapsedMs")
    task_id = operation.get("taskId")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_SAFE_INTEGER
        or operation.get("type") not in {"start", "pause", "resume", "finish", "cancel", "clear"}
        or operation.get("phase") not in {"focus", "short_break", "long_break"}
        or isinstance(planned, bool)
        or not isinstance(planned, int)
        or not 60_000 <= planned <= 14_400_000
        or isinstance(observed, bool)
        or not isinstance(observed, int)
        or not -MAX_SAFE_INTEGER <= observed <= MAX_SAFE_INTEGER
        or task_id is not None and (
            not valid_identifier(task_id)
            or operation.get("type") != "start"
            or operation.get("phase") != "focus"
        )
    ):
        raise IrohProtocolError("Timer operation is invalid.")
    _valid_peer_clock(operation)


def _validate_genesis_timer(timer: Any) -> None:
    if timer is not None and not Store._valid_canonical_timer(timer):
        raise IrohProtocolError("Genesis canonical timer is invalid.")
    if timer is None:
        return
    _exact_keys(
        timer,
        {
            "id",
            "phase",
            "status",
            "plannedDurationMs",
            "elapsedAtAnchorMs",
            "anchorAt",
        },
        {"taskId", "lastIntent", "startedByDeviceId"},
    )
    if not valid_identifier(timer["id"]):
        raise IrohProtocolError("Genesis timer identity is invalid.")
    for key in ("taskId", "startedByDeviceId"):
        if timer.get(key) is not None and not valid_identifier(timer[key]):
            raise IrohProtocolError("Genesis timer identity is invalid.")
    _validate_genesis_intent(timer.get("lastIntent"))


def _validate_genesis_intent(intent: Any) -> None:
    if intent is None:
        return
    if not isinstance(intent, dict):
        raise IrohProtocolError("Genesis timer intent is invalid.")
    _exact_keys(intent, {"type", "commandId", "occurredAt"}, {"deviceId"})
    if "deviceId" in intent:
        raise IrohProtocolError("Genesis timer intent contains a local origin field.")
    if not valid_identifier(intent["commandId"]) or (
        intent.get("deviceId") is not None
        and not valid_identifier(intent["deviceId"])
    ):
        raise IrohProtocolError("Genesis timer intent identity is invalid.")


def _validate_genesis_history(history: Any) -> None:
    if not isinstance(history, list) or any(not Store._valid_history_item(item) for item in history):
        raise IrohProtocolError("Genesis history is invalid.")
    for item in history:
        _exact_keys(
            item,
            {"id", "timerId", "phase", "status", "plannedDurationMs"},
            {"commandId", "taskId", "completedAt", "endedAt"},
        )
        for key in ("id", "timerId", "commandId", "taskId"):
            if item.get(key) is not None and not valid_identifier(item[key]):
                raise IrohProtocolError("Genesis history identity is invalid.")
        if any(
            key in item and item[key] is None
            for key in ("commandId", "taskId", "completedAt", "endedAt")
        ):
            raise IrohProtocolError("Genesis history is not canonical.")
    if len({item["id"] for item in history}) != len(history):
        raise IrohProtocolError("Genesis history contains duplicate IDs.")


def _validate_genesis_tasks(tasks: Any) -> None:
    if not isinstance(tasks, list):
        raise IrohProtocolError("Genesis tasks are invalid.")
    task_ids = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"id", "title"}:
            raise IrohProtocolError("Genesis tasks are invalid.")
        try:
            normalized = task_from_title(task["title"])
        except (KeyError, TypeError, ValueError) as error:
            raise IrohProtocolError("Genesis tasks are invalid.") from error
        if normalized != task:
            raise IrohProtocolError("Genesis task identity is invalid.")
        task_ids.append(task["id"])
    if len(task_ids) != len(set(task_ids)):
        raise IrohProtocolError("Genesis tasks contain duplicate IDs.")


def _validate_genesis_settings(operation: dict[str, Any]) -> None:
    try:
        Store._canonical_durations(operation["durationsMs"])
        wall, counter = Store._logical_clock(
            {"wallMs": operation["hlcWallMs"], "counter": operation["hlcCounter"]},
            allow_legacy_zero=True,
        )
    except ValueError as error:
        raise IrohProtocolError("Genesis durations or logical clock are invalid.") from error
    selected_task_id = operation["selectedTaskId"]
    if selected_task_id is not None and not valid_identifier(selected_task_id):
        raise IrohProtocolError("Genesis selected task is invalid.")
    if wall == 0 and counter != 0 or not isinstance(operation["autoStartBreaks"], bool):
        raise IrohProtocolError("Genesis settings or logical clock are invalid.")


def _validate_genesis(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {
            "canonicalTimer",
            "history",
            "tasks",
            "durationsMs",
            "autoStartBreaks",
            "selectedTaskId",
            "hlcWallMs",
            "hlcCounter",
        },
    )
    _validate_genesis_timer(operation["canonicalTimer"])
    _validate_genesis_history(operation["history"])
    _validate_genesis_tasks(operation["tasks"])
    _validate_genesis_settings(operation)


def _validate_timer_operation(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {
            "id",
            "deviceSequence",
            "timerId",
            "type",
            "phase",
            "plannedDurationMs",
            "occurredAt",
            "hlcWallMs",
            "hlcCounter",
            "observedElapsedMs",
        },
        {"taskId"},
    )
    if not valid_identifier(operation.get("timerId")) or (
        operation.get("taskId") is not None
        and not valid_identifier(operation["taskId"])
    ):
        raise IrohProtocolError("Timer operation identity is invalid.")
    _valid_peer_timer(operation)


def _validate_task_operation(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {"id", "taskId", "type", "occurredAt", "hlcWallMs", "hlcCounter"},
        {"title"},
    )
    if not valid_identifier(operation.get("taskId")):
        raise IrohProtocolError("Task operation identity is invalid.")
    row = {"id": operation["id"]}
    try:
        Store._validate_pending_task_operation(  # type: ignore[arg-type]
            Store.__new__(Store), operation, row
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IrohProtocolError("Task operation is invalid.") from error


def _validate_duration_operation(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {"id", "phase", "durationMs", "occurredAt", "hlcWallMs", "hlcCounter"},
    )
    row = {"id": operation["id"], "phase": operation.get("phase")}
    try:
        Store._validate_pending_duration_operation(  # type: ignore[arg-type]
            Store.__new__(Store), operation, row
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IrohProtocolError("Duration operation is invalid.") from error


def _validate_auto_start_operation(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {"id", "enabled", "occurredAt", "hlcWallMs", "hlcCounter"},
    )
    try:
        _valid_peer_clock(operation, allow_zero=True)
    except IrohProtocolError as error:
        raise IrohProtocolError("Auto-start operation is invalid.") from error
    if not isinstance(operation.get("enabled"), bool):
        raise IrohProtocolError("Auto-start operation is invalid.")


def _validate_selected_task_operation(operation: dict[str, Any]) -> None:
    _exact_keys(
        operation,
        {"id", "taskId", "occurredAt", "hlcWallMs", "hlcCounter"},
    )
    if operation.get("taskId") is not None and not valid_identifier(
        operation["taskId"]
    ):
        raise IrohProtocolError("Selected-task operation is invalid.")
    try:
        _valid_peer_clock(operation)
    except IrohProtocolError as error:
        raise IrohProtocolError("Selected-task operation is invalid.") from error


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise IrohProtocolError("Operation record must be an object.")
    _exact_keys(record, {"domain", "deviceId", "operation"})
    domain = record["domain"]
    device_id = record["deviceId"]
    operation = record["operation"]
    if domain not in DOMAINS or not valid_identifier(device_id) or not isinstance(operation, dict):
        raise IrohProtocolError("Operation record wrapper is invalid.")
    if len(canonical_json(record)) > MAX_OPERATION_BODY:
        raise IrohProtocolError("Operation exceeds 64 KiB.")
    if domain == "genesis":
        _validate_genesis(operation)
        return record
    if not valid_identifier(operation.get("id")):
        raise IrohProtocolError("Operation ID is invalid.")
    if domain == "timer":
        _validate_timer_operation(operation)
    elif domain == "task":
        _validate_task_operation(operation)
    elif domain == "duration":
        _validate_duration_operation(operation)
    elif domain == "autoStart":
        _validate_auto_start_operation(operation)
    else:
        _validate_selected_task_operation(operation)
    return record


def record_id(record: dict[str, Any]) -> str:
    return "genesis" if record["domain"] == "genesis" else str(record["operation"]["id"])


def operation_order(record: dict[str, Any]) -> tuple[int, int, bytes, bytes]:
    operation = record["operation"]
    return (
        int(operation["hlcWallMs"]),
        int(operation["hlcCounter"]),
        record["deviceId"].encode(),
        record_id(record).encode(),
    )


def encode_message(message: dict[str, Any]) -> bytes:
    validate_message(message)
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()


def decode_message(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_FRAME_BODY:
        raise IrohProtocolError("Message exceeds 16 MiB.")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IrohProtocolError("Message body must be UTF-8 JSON.") from error
    return validate_message(value)


def _valid_reference(reference: Any) -> bool:
    return (
        isinstance(reference, dict)
        and set(reference) == {"domain", "id"}
        and reference["domain"] in DOMAINS
        and (reference["id"] == "genesis" if reference["domain"] == "genesis" else valid_identifier(reference["id"]))
    )


def _valid_cursor(cursor: Any) -> bool:
    if cursor is None:
        return True
    if not isinstance(cursor, str) or cursor.count("\0") != 1:
        return False
    domain, identifier = cursor.split("\0")
    return _valid_reference({"domain": domain, "id": identifier})


def _validate_hello_message(message: dict[str, Any], base: set[str]) -> None:
    required = base | {"deviceId", "endpointTicket", "platform"}
    _exact_keys(message, required, {"displayName"})
    display_name = message.get("displayName")
    if (
        not valid_identifier(message["deviceId"])
        or not isinstance(message["endpointTicket"], str)
        or not message["endpointTicket"]
        or (_utf8_length(message["endpointTicket"]) or MAX_ENDPOINT_TICKET + 1)
        > MAX_ENDPOINT_TICKET
        or message["platform"]
        not in {"ios", "macos", "android", "linux", "windows"}
        or display_name is not None
        and not _valid_room_name(display_name)
    ):
        raise IrohProtocolError("Hello fields are invalid.")


def _validate_inventory_message(message: dict[str, Any], base: set[str]) -> None:
    _exact_keys(message, base | {"after", "limit"})
    if (
        isinstance(message["limit"], bool)
        or not isinstance(message["limit"], int)
        or not 1 <= message["limit"] <= MAX_INVENTORY
        or not _valid_cursor(message["after"])
    ):
        raise IrohProtocolError("Inventory request is invalid.")


def _inventory_entry_key(
    entry: Any,
    previous: tuple[bytes, bytes] | None,
    seen: set[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"domain", "id", "digest"}
        or not _valid_reference(
            {"domain": entry.get("domain"), "id": entry.get("id")}
        )
    ):
        raise IrohProtocolError("Inventory entry is invalid.")
    try:
        digest = b64url_decode(entry["digest"], label="record digest")
    except (IrohProtocolError, TypeError) as error:
        raise IrohProtocolError("Inventory digest is invalid.") from error
    key = (entry["domain"].encode(), entry["id"].encode())
    if len(digest) != 32 or key in seen or previous is not None and key <= previous:
        raise IrohProtocolError("Inventory entries are duplicate or out of order.")
    return key


def _validate_inventory_result(
    message: dict[str, Any], base: set[str]
) -> None:
    _exact_keys(message, base | {"entries", "next"})
    entries = message["entries"]
    if (
        not isinstance(entries, list)
        or len(entries) > MAX_INVENTORY
        or not _valid_cursor(message["next"])
    ):
        raise IrohProtocolError("Inventory result is invalid.")
    previous: tuple[bytes, bytes] | None = None
    seen: set[tuple[bytes, bytes]] = set()
    for entry in entries:
        previous = _inventory_entry_key(entry, previous, seen)
        seen.add(previous)


def _validate_operations_message(
    message: dict[str, Any], base: set[str]
) -> None:
    _exact_keys(message, base | {"refs"})
    refs = message["refs"]
    keys = (
        []
        if not isinstance(refs, list)
        else [
            (item.get("domain"), item.get("id"))
            for item in refs
            if isinstance(item, dict)
        ]
    )
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= MAX_OPERATION_REFS
        or any(not _valid_reference(item) for item in refs)
        or len(set(keys)) != len(refs)
    ):
        raise IrohProtocolError("Operation references are invalid.")


def _validate_operations_result(
    message: dict[str, Any], base: set[str]
) -> None:
    _exact_keys(message, base | {"records"})
    records = message["records"]
    if not isinstance(records, list) or len(records) > MAX_OPERATION_REFS:
        raise IrohProtocolError("Operation result is invalid.")
    keys = []
    for record in records:
        validate_record(record)
        keys.append((record["domain"], record_id(record)))
    if len(keys) != len(set(keys)):
        raise IrohProtocolError("Operation result contains duplicate records.")


def _validate_error_message(message: dict[str, Any], base: set[str]) -> None:
    _exact_keys(message, base | {"code", "message", "retryable"})
    message_size = _utf8_length(message["message"])
    if (
        message["code"] not in ERROR_CODES
        or message_size is None
        or message_size > 1024
        or not isinstance(message["retryable"], bool)
    ):
        raise IrohProtocolError("Error response is invalid.")


def validate_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise IrohProtocolError("Message must be an object.")
    base = {"protocolVersion", "roomId", "requestId", "kind"}
    if not base <= set(message):
        raise IrohProtocolError("Message envelope is incomplete.")
    if (
        isinstance(message["protocolVersion"], bool)
        or message["protocolVersion"] != PROTOCOL_VERSION
    ):
        raise IrohProtocolError("Protocol version is unsupported.")
    if not valid_room_id(message["roomId"]) or not valid_request_id(
        message["requestId"]
    ):
        raise IrohProtocolError("Message room or request ID is invalid.")
    kind = message["kind"]
    validators = {
        "hello": _validate_hello_message,
        "inventory": _validate_inventory_message,
        "inventoryResult": _validate_inventory_result,
        "operations": _validate_operations_message,
        "operationsResult": _validate_operations_result,
        "error": _validate_error_message,
    }
    validator = validators.get(kind) if isinstance(kind, str) else None
    if validator is None:
        raise IrohProtocolError("Message kind is unknown.")
    validator(message, base)
    return message
