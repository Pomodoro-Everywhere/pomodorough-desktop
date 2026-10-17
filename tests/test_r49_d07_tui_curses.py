"""Exercise the production writer in a real curses pseudoterminal."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _read_child(child, master, report_fd):
    report, terminal = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(master, selectors.EVENT_READ, terminal)
        selector.register(report_fd, selectors.EVENT_READ, report)
        deadline = time.monotonic() + 30
        while selector.get_map() and time.monotonic() < deadline:
            for key, _ in selector.select(0.1):
                try:
                    fragment = os.read(key.fd, 65536)
                except OSError:
                    fragment = b""
                if fragment:
                    key.data.extend(fragment)
                else:
                    selector.unregister(key.fd)
    if child.poll() is None:
        child.kill()
    assert child.wait(timeout=5) == 0, terminal.decode(errors="replace")[-2000:]
    return json.loads(report)


@pytest.fixture(scope="module")
def native_results(tmp_path_factory):
    if os.name == "nt":
        pytest.skip("POSIX pseudoterminal requires Unix")
    import pty

    master, slave = pty.openpty()
    report_fd, write_fd = os.pipe()
    directory = tmp_path_factory.mktemp("r49-d07-curses")
    environment = os.environ | {"TERM": "xterm-256color", "LANG": "en_US.UTF-8",
                                "R49_REPORT_FD": str(write_fd), "POMODOROUGH_SENTRY_DISABLE": "1"}
    script = Path(__file__).with_name("r49_d07_curses_child.py")
    child = subprocess.Popen([sys.executable, str(script), str(directory)], env=environment,
                             stdin=slave, stdout=slave, stderr=slave, pass_fds=(write_fd,))
    os.close(slave)
    os.close(write_fd)
    try:
        return _read_child(child, master, report_fd)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        os.close(master)
        os.close(report_fd)


@pytest.mark.parametrize("section", ["widths", "roomy", "graphemes", "controls", "quit_loop"])
def test_native_curses_rendering_boundaries(native_results, section):
    cases = native_results[section]
    assert cases
    for case in cases:
        assert not case.get("error"), case
        assert case["sql_unchanged"], case
        assert case["quit_present"], case
        assert case["feedback_present"], case
        assert all(start == end and column < case["width"]
                   for start, end, column in case["writes"]), case
        if section == "quit_loop":
            assert case["reads"] == case["expected_reads"], case
            assert case["capture_count"] == case["expected_captures"], case
        if section == "roomy":
            assert case["end_present"] and not case["ellipsis"], case


def test_native_one_cell_fallback_consumes_whole_grapheme(native_results):
    for case in native_results["one_cell"]:
        expected = case["cluster"] if case["cluster"] in {"e\u0301", "❤\ufe0e"} else "?"
        assert case["rows"] == [expected], case
        assert expected in case["buffer"], case
        assert case["cursor"] == [0, 1], case
