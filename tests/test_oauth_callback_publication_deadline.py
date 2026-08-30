from __future__ import annotations

import queue
import threading
import time

import pytest

from pomodorough import network
from test_oauth_callback_read_bounds_p218 import callback_flow as callback_flow


@pytest.fixture
def publication_pause():
    entered = threading.Event()
    release = threading.Event()

    def pause():
        entered.set()
        assert release.wait(2), "Publication probe was not released"

    yield entered, release, pause
    release.set()


def pause_query_processing(monkeypatch, phase, pause):
    original_parse = network.urllib.parse.parse_qs

    class PausedValues(list):
        def __getitem__(self, index):
            pause()
            return super().__getitem__(index)

    def parse_query(*arguments, **keywords):
        if phase == "query-parse":
            pause()
        query = original_parse(*arguments, **keywords)
        if phase == "result-build":
            query["code"] = PausedValues(query["code"])
        return query

    monkeypatch.setattr(network.urllib.parse, "parse_qs", parse_query)


def expire_callback(flow):
    deadline = flow.servers[0].RequestHandlerClass.deadline
    assert not flow.finished.wait(max(0, deadline - time.monotonic()) + 0.02)
    assert time.monotonic() >= deadline


def assert_lifecycle_error(flow, error_key):
    flow.assert_finished(network._text(error_key))
    assert type(flow.errors[0]) is network.ApiError
    assert str(flow.errors[0]) == network._text(error_key)
    assert all(accepted.fileno() == -1 for accepted, _client in flow.pairs)


@pytest.mark.parametrize("phase", ["query-parse", "result-build"])
@pytest.mark.parametrize("stop", ["deadline", "cancel", "both"])
def test_lifecycle_rechecked_after_query_and_result_construction(
    callback_flow, monkeypatch, publication_pause, phase, stop,
):
    entered, release, pause = publication_pause
    pause_query_processing(monkeypatch, phase, pause)
    flow = callback_flow(timeout=0.2 if stop != "cancel" else 10)
    try:
        flow.start()
        assert entered.wait(1)
        assert flow.servers[0].RequestHandlerClass.result_queue.empty()
        if stop != "cancel":
            expire_callback(flow)
        if stop != "deadline":
            flow.transport.cancel()
    finally:
        release.set()
    error_key = (
        "network.error.sign_in_timeout" if stop == "deadline"
        else "network.error.sign_in_cancelled"
    )
    assert_lifecycle_error(flow, error_key)
    assert flow.servers[0].RequestHandlerClass.result_queue.empty()


@pytest.mark.parametrize("stop", ["deadline", "cancel", "both"])
def test_accepted_callback_survives_late_response_unless_cancelled(
    callback_flow, monkeypatch, publication_pause, stop,
):
    entered, release, pause = publication_pause
    original_response = network._CallbackHandler.send_response

    def delayed_response(handler, *arguments, **keywords):
        pause()
        return original_response(handler, *arguments, **keywords)

    monkeypatch.setattr(network._CallbackHandler, "send_response", delayed_response)
    flow = callback_flow(timeout=0.2 if stop != "cancel" else 10)
    try:
        flow.start()
        assert entered.wait(1)
        handler = flow.servers[0].RequestHandlerClass
        assert not handler.result_queue.empty()
        assert time.monotonic() < handler.deadline
        if stop != "cancel":
            expire_callback(flow)
        if stop != "deadline":
            flow.transport.cancel()
    finally:
        release.set()
    if stop != "deadline":
        assert_lifecycle_error(flow, "network.error.sign_in_cancelled")
    else:
        flow.assert_finished()
        assert flow.responses == [(
            "http://127.0.0.1:43123/callback", {"state": "expected", "code": "accepted"},
        )]
        assert b"HTTP/1.0 200 OK\r\n" in flow.pairs[0][1].recv(4096)


@pytest.mark.parametrize("timeout", [0.004, 0.008, 0.012])
def test_large_valid_query_has_no_late_publication(callback_flow, monkeypatch, timeout):
    published = []
    original_queue = queue.Queue

    class ObservedQueue(original_queue):
        def put(self, item, *arguments, **keywords):
            published.append(time.monotonic())
            return super().put(item, *arguments, **keywords)

    monkeypatch.setattr(network.queue, "Queue", ObservedQueue)
    request = (
        b"GET /callback?state=expected&code=accepted&"
        + b"x=1&" * 15000 + b"z=1 HTTP/1.1\r\n\r\n"
    )
    assert len(request) < 65536
    flow = callback_flow([request], timeout=timeout)
    flow.start()
    assert flow.finished.wait(0.5)
    deadline = flow.servers[0].RequestHandlerClass.deadline
    assert all(moment <= deadline for moment in published)
    if flow.responses:
        flow.assert_finished()
        assert len(published) == 1
        assert flow.responses == [(
            "http://127.0.0.1:43123/callback",
            {"state": "expected", "code": "accepted", "x": "1", "z": "1"},
        )]
    else:
        assert_lifecycle_error(flow, "network.error.sign_in_timeout")
        assert published == []
