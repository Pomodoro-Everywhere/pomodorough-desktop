from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from http.server import HTTPServer
from unittest.mock import Mock

import pytest

from pomodorough import network


VALID_REQUEST = (
    b"GET /callback?state=expected&code=accepted HTTP/1.1\r\n"
    b"Host: localhost\r\n\r\n"
)
PARTIAL_REQUESTS = (
    b"GET /callback?state=expected&code=accepted HTTP/1.1",
    b"GET /callback?state=expected&code=accepted HTTP/1.1\r\nHost: localhost\r\nX-Test:",
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class DripConnection:
    def __init__(self, clock: ManualClock, interval: float) -> None:
        self.clock = clock
        self.interval = interval
        self.sent = 0
        self.timeouts = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recv_into(self, buffer: bytearray) -> int:
        self.clock.now += self.interval
        buffer[0] = ord("x")
        self.sent += 1
        return 1


class UnboundCallbackServer(HTTPServer):
    def __init__(self, handler, connections, entered):
        super().__init__(("127.0.0.1", 0), handler, bind_and_activate=False)
        self.server_port = 43123
        self.connections = iter(connections)
        self.pending = len(connections)
        self.entered = entered
        self.errors = []

    def handle_request(self):
        if self.pending:
            self._handle_request_noblock()
        else:
            time.sleep(min(self.timeout, 0.01))

    def get_request(self):
        self.pending -= 1
        self.entered.set()
        return next(self.connections), ("127.0.0.1", 12345)

    def handle_error(self, request, client_address):
        self.errors.append((request, client_address))


class CallbackFlow:
    def __init__(self, monkeypatch, requests, timeout, transport=None, opener=None):
        self.pairs = [socket.socketpair() for _ in requests]
        self.requests = requests
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.responses = []
        self.errors = []
        self.servers = []
        self.opener = opener or Mock(return_value=True)
        self.transport = transport or network.SystemOAuthBrowserTransport(
            open_browser=self.opener, callback_timeout=timeout,
        )
        self.worker = threading.Thread(target=self.authorize, daemon=True)
        for _, client in self.pairs:
            client.settimeout(0.4)
        monkeypatch.setattr(network, "HTTPServer", self.create_server)

    def create_server(self, address, handler):
        server = UnboundCallbackServer(
            handler, [accepted for accepted, _ in self.pairs], self.entered,
        )
        self.servers.append(server)
        return server

    def authorize(self):
        try:
            self.responses.append(self.transport.authorize(lambda redirect: redirect))
        except Exception as error:
            self.errors.append(error)
        finally:
            self.finished.set()

    def start(self, after_send: Callable[[], None] | None = None):
        self.worker.start()
        for (_, client), request in zip(self.pairs, self.requests):
            if request:
                try:
                    client.sendall(request)
                except (BrokenPipeError, TimeoutError):
                    if not self.callback_timed_out():
                        raise
        if after_send is not None:
            after_send()
        assert self.entered.wait(1)

    def callback_timed_out(self):
        self.worker.join(0.4)
        return (
            len(self.errors) == 1
            and isinstance(self.errors[0], network.ApiError)
            and str(self.errors[0]) == network._text("network.error.sign_in_timeout")
        )

    def assert_finished(self, message=None):
        assert self.finished.wait(0.4), "Callback handling did not stop independently"
        self.worker.join(1)
        assert not self.worker.is_alive()
        assert all(server.socket.fileno() == -1 for server in self.servers)
        assert all(not server.errors for server in self.servers)
        if message is None:
            assert not self.errors
        else:
            assert not self.responses
            assert len(self.errors) == 1
            assert isinstance(self.errors[0], network.ApiError)
            assert message in str(self.errors[0])

    def close(self):
        self.transport.cancel()
        if self.worker.ident is not None:
            self.worker.join(1)
        worker_is_alive = self.worker.is_alive()
        for accepted, client in self.pairs:
            client.close()
            accepted.close()
        assert not worker_is_alive


@pytest.fixture
def callback_flow(monkeypatch):
    flows = []

    def create(requests=(VALID_REQUEST,), timeout=0.15, **options):
        flow = CallbackFlow(monkeypatch, requests, timeout, **options)
        flows.append(flow)
        return flow

    yield create
    for flow in reversed(flows):
        flow.close()


def test_flow_surfaces_write_failure_before_callback_timeout(callback_flow):
    flow = callback_flow(timeout=10)
    accepted, client = flow.pairs[0]

    class FailedClient:
        def sendall(self, _request):
            raise BrokenPipeError("fixture write failed")

        def close(self):
            client.close()

    flow.pairs[0] = accepted, FailedClient()
    with pytest.raises(BrokenPipeError, match="fixture write failed"):
        flow.start()


@pytest.mark.parametrize("partial", PARTIAL_REQUESTS, ids=["partial-line", "partial-headers"])
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_partial_reads_stop_without_peer_completion(callback_flow, partial, stop):
    flow = callback_flow([partial], timeout=0.12 if stop == "deadline" else 180)
    flow.start()
    if stop == "cancel":
        flow.transport.cancel()
    flow.assert_finished("timed out" if stop == "deadline" else "cancelled")
    assert flow.pairs[0][0].fileno() == -1


@pytest.mark.parametrize(("interval", "expected"), [(1 / 32, 4), (1 / 64, 8)])
def test_slow_drip_cannot_extend_absolute_deadline(monkeypatch, interval, expected):
    clock = ManualClock()
    connection = DripConnection(clock, interval)
    monkeypatch.setattr(network, "time", clock)
    reader = network._CallbackReader(connection, interval * expected, threading.Event())
    buffer = bytearray(1)

    for _ in range(expected):
        assert reader.readinto(buffer) == 1
    with pytest.raises(TimeoutError):
        reader.readinto(buffer)

    assert connection.sent == expected
    assert connection.sent >= 3
    assert len(connection.timeouts) == expected
    assert connection.timeouts[-1] == interval
    assert all(0 < timeout <= 0.05 for timeout in connection.timeouts)


@pytest.mark.parametrize("partial", PARTIAL_REQUESTS, ids=["partial-line", "partial-headers"])
def test_idle_poll_timeout_does_not_poison_buffered_reader(callback_flow, partial):
    flow = callback_flow([partial], timeout=1)
    flow.start()
    assert not flow.finished.wait(0.12)
    flow.pairs[0][1].sendall(b"\r\n\r\n")
    flow.assert_finished()
    assert flow.responses == [(
        "http://127.0.0.1:43123/callback", {"state": "expected", "code": "accepted"},
    )]


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_buffered_callback_rechecks_lifecycle_before_publication(callback_flow, monkeypatch, stop):
    entered = threading.Event()
    release = threading.Event()
    original = network._CallbackHandler.do_GET

    def delayed_callback(handler):
        entered.set()
        assert release.wait(1)
        original(handler)

    monkeypatch.setattr(network._CallbackHandler, "do_GET", delayed_callback)
    flow = callback_flow(timeout=0.12 if stop == "deadline" else 180)
    flow.start()
    try:
        assert entered.wait(1)
        if stop == "cancel":
            flow.transport.cancel()
        else:
            assert not flow.finished.wait(0.16)
    finally:
        release.set()
    flow.assert_finished("timed out" if stop == "deadline" else "cancelled")


@pytest.mark.parametrize(
    "query, expected_code, error",
    [
        ("state=expected&code=%20accepted%20", "accepted", None),
        ("state=expected&code=first&code=second", "first", None),
        ("state=wrong&code=accepted", None, "invalid state"),
        ("code=accepted", None, "invalid state"),
        ("state=&code=accepted", None, "invalid state"),
        ("state=expected&error=access_denied", None, "access_denied"),
        ("state=expected&error=no&error_description=Denied", None, "Denied"),
        ("state=expected&code=%20%20", None, "cancelled"),
        ("state=expected", None, "cancelled"),
    ],
)
def test_complete_callbacks_preserve_state_and_error_validation(
    callback_flow, query, expected_code, error,
):
    request = f"GET /callback?{query} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
    flow = callback_flow([request])
    flow.start()
    flow.assert_finished()
    redirect, callback = flow.responses[0]
    assert redirect == "http://127.0.0.1:43123/callback"
    if error is None:
        assert network.DesktopOAuthContract.authorization_code(callback, "expected") == expected_code
    else:
        with pytest.raises(network.ApiError, match=error):
            network.DesktopOAuthContract.authorization_code(callback, "expected")
    response = flow.pairs[0][1].recv(4096)
    assert b"HTTP/1.0 200 OK\r\n" in response
    assert b"Content-Type: text/html; charset=utf-8\r\n" in response


@pytest.mark.parametrize(
    "malformed, status",
    [
        (b"GET /callback HTTP/invalid\r\n\r\n", b"400"),
        (b"POST /callback HTTP/1.1\r\n\r\n", b"501"),
        (b"GET /" + b"x" * 65537 + b"\r\n\r\n", b"414"),
        (b"GET /callback HTTP/1.1\r\n" + b"X: y\r\n" * 101 + b"\r\n", b"431"),
    ],
    ids=["malformed-version", "unsupported-method", "long-line", "too-many-headers"],
)
def test_malformed_request_does_not_prevent_next_valid_callback(callback_flow, malformed, status):
    flow = callback_flow([malformed, VALID_REQUEST], timeout=1)
    flow.start()
    flow.assert_finished()
    assert flow.responses[0][1] == {"state": "expected", "code": "accepted"}
    assert status in flow.pairs[0][1].recv(4096)
    assert all(accepted.fileno() == -1 for accepted, _ in flow.pairs)


def test_peer_eof_does_not_prevent_next_valid_callback(callback_flow):
    flow = callback_flow([b"", VALID_REQUEST], timeout=1)
    flow.pairs[0][1].shutdown(socket.SHUT_WR)
    flow.start()
    flow.assert_finished()
    assert flow.responses[0][1] == {"state": "expected", "code": "accepted"}


@pytest.mark.parametrize("first_request", [VALID_REQUEST, PARTIAL_REQUESTS[0]])
def test_transport_reuse_after_success_or_timeout(callback_flow, first_request):
    first = callback_flow([first_request])
    first.start()
    first.assert_finished(None if first_request == VALID_REQUEST else "timed out")
    second = callback_flow(transport=first.transport)
    second.start()
    second.assert_finished()
    assert second.responses[0][1] == {"state": "expected", "code": "accepted"}


def test_repeated_cancel_is_terminal_and_close_is_safe(callback_flow):
    first = callback_flow([PARTIAL_REQUESTS[1]], timeout=180)
    first.start()
    for _ in range(10):
        first.transport.cancel()
    first.assert_finished("cancelled")
    second = callback_flow(transport=first.transport)
    second.worker.start()
    second.assert_finished("cancelled")
    assert not second.entered.is_set()
    first.transport.cancel()


def test_cancel_after_queued_callback_wins_and_closes_streams(callback_flow, monkeypatch):
    flow = callback_flow()
    original = network._CallbackHandler.finish

    def cancel_before_finish(handler):
        assert not handler.result_queue.empty()
        flow.transport.cancel()
        original(handler)

    monkeypatch.setattr(network._CallbackHandler, "finish", cancel_before_finish)
    flow.start()
    flow.assert_finished("cancelled")
    assert flow.pairs[0][0].fileno() == -1


@pytest.mark.parametrize("opened", [False, RuntimeError("opener failed")])
def test_opener_failure_closes_unbound_server(callback_flow, opened):
    opener = Mock(side_effect=opened) if isinstance(opened, Exception) else Mock(return_value=opened)
    flow = callback_flow(opener=opener)
    flow.worker.start()
    assert flow.finished.wait(1)
    flow.worker.join(1)
    assert not flow.responses
    assert len(flow.errors) == 1
    assert all(server.socket.fileno() == -1 for server in flow.servers)
    assert not flow.entered.is_set()


@pytest.mark.parametrize("partial", [None, *PARTIAL_REQUESTS])
def test_handler_closes_replaced_buffer_and_reader(callback_flow, monkeypatch, partial):
    readers = []
    original = network._CallbackHandler.setup

    def record_reader(handler):
        original(handler)
        readers.append((handler.rfile, handler.callback_reader))

    monkeypatch.setattr(network._CallbackHandler, "setup", record_reader)
    flow = callback_flow([partial or VALID_REQUEST])
    flow.start()
    flow.assert_finished("timed out" if partial else None)
    assert len(readers) == 1
    assert all(buffer.closed and raw.closed for buffer, raw in readers)
