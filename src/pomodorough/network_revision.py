from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


class RevisionEventParser:
    def __init__(self) -> None:
        self.buffer = b""
        self.data_lines: list[bytes] = []

    def feed(self, chunk: bytes) -> list[int]:
        self.buffer += chunk
        revisions: list[int] = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                revision = self._dispatch()
                if revision is not None:
                    revisions.append(revision)
            elif line.startswith(b"data:"):
                data = line[5:]
                if data.startswith(b" "):
                    data = data[1:]
                self.data_lines.append(data)
        return revisions

    def _dispatch(self) -> int | None:
        if not self.data_lines:
            return None
        raw = b"\n".join(self.data_lines)
        self.data_lines = []
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                document = raw.decode()
            except UnicodeDecodeError:
                return None
        value = document.get("revision") if isinstance(document, dict) else document
        if isinstance(value, bool):
            return None
        try:
            revision = int(value)
        except (TypeError, ValueError):
            return None
        return revision if revision >= 0 else None


@dataclass
class RevisionStreamState:
    reply: QNetworkReply | None = None
    parser: RevisionEventParser = field(default_factory=RevisionEventParser)
    reconnect_attempt: int = 0


@dataclass(frozen=True)
class RevisionFinish:
    was_active: bool
    status: Any = None
    revisions: tuple[int, ...] = ()


class RevisionStream:
    def __init__(
        self,
        parent: QObject,
        api_base: str,
        reconnect: Callable[[], None],
        randbelow: Callable[[int], int],
        valid_response: Callable[[QNetworkReply], bool] | None = None,
    ) -> None:
        self.api_base = api_base
        self.state = RevisionStreamState()
        self.network: Any = QNetworkAccessManager(parent)
        self.reconnect_timer = QTimer(parent)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(reconnect)
        self.randbelow = randbelow
        self.response_is_valid = valid_response or self.valid_response

    def start(
        self,
        access_token: str,
        ready_read: Callable[[QNetworkReply], None],
        finished: Callable[[QNetworkReply], None],
    ) -> None:
        self.reconnect_timer.stop()
        request = QNetworkRequest(QUrl(f"{self.api_base}/api/v1/stream"))
        request.setRawHeader(b"Accept", b"text/event-stream")
        request.setRawHeader(b"Authorization", f"Bearer {access_token}".encode())
        reply = self.network.get(request)
        self.state.reply = reply
        self.state.parser = RevisionEventParser()
        reply.readyRead.connect(lambda reply=reply: ready_read(reply))
        reply.finished.connect(lambda reply=reply: finished(reply))

    def read(self, reply: QNetworkReply) -> tuple[int, ...]:
        if reply is not self.state.reply:
            return ()
        if not self.response_is_valid(reply):
            reply.readAll()
            return ()
        revisions = tuple(self.state.parser.feed(bytes(reply.readAll())))
        if revisions:
            self.state.reconnect_attempt = 0
        return revisions

    @staticmethod
    def valid_response(reply: QNetworkReply) -> bool:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        content_type = bytes(reply.rawHeader("Content-Type"))
        media_type = content_type.split(b";", 1)[0].strip().lower()
        return status == 200 and media_type == b"text/event-stream"

    def finish(self, reply: QNetworkReply) -> RevisionFinish:
        if reply is not self.state.reply:
            reply.deleteLater()
            return RevisionFinish(False)
        revisions = self.read(reply)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        self.state.reply = None
        reply.deleteLater()
        return RevisionFinish(True, status, revisions)

    def schedule_reconnect(self) -> None:
        exponent = min(self.state.reconnect_attempt, 5)
        base_ms = min(20_000, 1_000 * (2**exponent))
        jitter_ms = self.randbelow(min(10_000, base_ms // 2) + 1)
        self.state.reconnect_attempt += 1
        self.reconnect_timer.start(min(30_000, base_ms + jitter_ms))

    def stop(self) -> None:
        self.reconnect_timer.stop()
        self.state.reconnect_attempt = 0
        reply = self.state.reply
        self.state.reply = None
        self.state.parser = RevisionEventParser()
        if reply is not None:
            reply.abort()
            reply.deleteLater()
