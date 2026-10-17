"""Native curses child for R49-D07. Databases and reports stay in temporary storage."""

from __future__ import annotations

import curses
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pomodorough import tui
from pomodorough.core import task_from_title
from pomodorough.localization import Strings
from pomodorough.storage import Store
from pomodorough.terminal import LocalTimer


CLUSTERS = ["界", "🙂", "e\u0301", "👩\u200d💻", "👍🏽", "🇺🇸", "❤️", "1️⃣", "❤\ufe0e"]
CONTROLS = ["\x00", "\x07", "\x08", "\x09", "\x0a", "\x0d", "\x1b", "\x7f",
            "\x85", "\x9b", "\u200e", "\u202e"]


class NativeScreen:
    def __init__(self, height, width, keys=()):
        self.window = curses.newwin(height, width, 0, 0)
        self.writes = []
        self.reads = 0
        self.keys = list(keys)
        self.frames = []

    def __getattr__(self, name):
        return getattr(self.window, name)

    def addnstr(self, row, column, text, limit, *args):
        height, width = self.window.getmaxyx()
        next_row = self.window.instr(row + 1, 0) if row + 1 < height else None
        self.window.addnstr(row, column, text, limit, *args)
        end_row, end_column = self.window.getyx()
        self.writes.append((row, end_row, end_column))
        assert 0 <= row < height and end_row == row and end_column < width
        if next_row is not None:
            assert self.window.instr(row + 1, 0) == next_row
        assert not any(char in text for char in CONTROLS)

    def refresh(self):
        self.window.refresh()
        height, _ = self.window.getmaxyx()
        self.frames.append("\n".join(self.window.instr(row, 0).decode("utf-8")
                                     for row in range(height)))

    def getch(self):
        self.reads += 1
        key = self.keys.pop(0)
        if key == -1:
            return -1
        curses.ungetch(key)
        return self.window.getch()


def _dump(store):
    return "\n".join(store.connection.iterdump())


def _start(timer, title):
    task = task_from_title(title)
    timer.store.queue_task_operation("upsert", task)
    timer.store.set_selected_task_id(task["id"])
    timer.issue("start")
    assert timer.state()["display"]["taskTitle"] == task["title"]


def _observation(timer, screen, before, error=""):
    frame = screen.frames[-1] if screen.frames else ""
    quit_text = "q " + timer.strings.text("tui.keys_secondary").split("q ", 1)[1]
    return {"width": screen.getmaxyx()[1], "writes": screen.writes, "error": error,
            "sql_unchanged": _dump(timer.store) == before,
            "quit_present": all(quit_text in frame for frame in screen.frames) and bool(frame),
            "feedback_present": "Cannot pause now" in frame or "disk?unavailable" in frame,
            "end_present": "END" in frame, "ellipsis": "…" in frame}


def _draw_case(timer, shape, message, fallback=False):
    before = _dump(timer.store)
    screen = NativeScreen(*shape)
    error = ""
    try:
        if fallback:
            failure = OSError(message)
            tui._draw_failure(screen, failure, timer.strings)
            assert str(failure) == message
        else:
            tui._draw(screen, timer, message)
    except Exception as failure:
        error = repr(failure)
    return _observation(timer, screen, before, error)


def _width_cases(directory, results):
    for locale in ("en", "ar-XB"):
        with closing(Store(directory / f"width-{locale}.sqlite3")) as store:
            timer = LocalTimer(store, strings=Strings(locale))
            _start(timer, "x" * 512)
            store.prepare_resolution({"id": "user-1"}, 4, "merge")
            for width in range(40, 81):
                for fallback in (False, True):
                    case = _draw_case(timer, (12, width), "Cannot pause now", fallback)
                    results["widths"].append(case | {"locale": locale, "fallback": fallback})


def _roomy_cases(directory, results):
    for locale in ("en", "ar-XB"):
        with closing(Store(directory / f"roomy-{locale}.sqlite3")) as store:
            timer = LocalTimer(store, strings=Strings(locale))
            for fallback in (False, True):
                message = "Cannot pause now. " + "detail " * 225 + "END"
                case = _draw_case(timer, (40, 80), message, fallback)
                results["roomy"].append(case | {"locale": locale, "fallback": fallback})


def _text_cases(directory, results):
    for section, titles in (("graphemes", [cluster * 40 for cluster in CLUSTERS]),
                            ("controls", ["a" + control + "b" for control in CONTROLS])):
        for index, title in enumerate(titles):
            with closing(Store(directory / f"{section}-{index}.sqlite3")) as store:
                timer = LocalTimer(store)
                _start(timer, title)
                for width in (40, 54, 62, 80):
                    for fallback in (False, True):
                        case = _draw_case(timer, (12, width), "Cannot pause now " + title * 4, fallback)
                        results[section].append(case | {"title": title, "fallback": fallback})
                for row in tui._wrap_line(title, 40):
                    screen = NativeScreen(2, 40)
                    screen.addnstr(0, 0, row, len(row))
                    # macOS ncurses discards ZWJ in its buffer, but keeps both glyphs.
                    buffer = screen.window.instr(0, 0).decode("utf-8")
                    assert row.replace("\u200d", "") in buffer.replace("\u200d", "")
                assert timer.state()["display"]["taskTitle"] == task_from_title(title)["title"]


def _pending_loop(timer, width):
    keys = [-1] + [ord(key) for key in " fFxXcC123+=-_?"] + [ord("Q")]
    screen = NativeScreen(12, width, keys)
    captures = []
    before = _dump(timer.store)
    with patch.object(tui, "capture_exception", captures.append), patch.object(tui.time, "sleep"):
        tui._run(screen, timer)
    case = _observation(timer, screen, before)
    case["feedback_present"] = any("Resolve pending account history" in frame for frame in screen.frames)
    return case | {"reads": screen.reads, "expected_reads": len(keys),
                   "capture_count": len(captures), "expected_captures": 0}


def _one_cell_cases(results):
    for cluster in CLUSTERS:
        screen = NativeScreen(2, 2)
        rows = tui._wrap_line(cluster, 2)
        for row, text in enumerate(rows):
            screen.addnstr(row, 0, text, len(text))
        # NativeScreen checks the next row with instr(), which moves the cursor.
        cursor = screen.writes[-1][1:]
        results["one_cell"].append({"cluster": cluster, "rows": rows,
                                    "buffer": screen.window.instr(0, 0).decode("utf-8"),
                                    "cursor": cursor})


def _loop_cases(directory, results):
    for locale, width in (("en", 54), ("ar-XB", 62)):
        with closing(Store(directory / f"loop-{locale}.sqlite3")) as store:
            timer = LocalTimer(store, strings=Strings(locale))
            _start(timer, "a\x00b")
            before = _dump(store)
            screen = NativeScreen(12, width, [ord("q")])
            captures = []
            failure = OSError("disk\x00unavailable")
            error = ""
            with patch.object(timer, "state", side_effect=failure), patch.object(tui, "capture_exception", captures.append):
                try:
                    tui._run(screen, timer)
                except Exception as caught:
                    error = repr(caught)
            assert str(failure) == "disk\x00unavailable"
            assert not captures or captures[0] is failure
            case = _observation(timer, screen, before, error)
            results["quit_loop"].append(case | {"reads": screen.reads, "expected_reads": 1,
                                               "capture_count": len(captures), "expected_captures": 1})
            store.prepare_resolution({"id": "user-1"}, 4, "merge")
            results["quit_loop"].append(_pending_loop(timer, width))


def main():
    directory = Path(sys.argv[1])
    results = {section: [] for section in ("widths", "roomy", "graphemes", "controls", "quit_loop", "one_cell")}

    def execute(_screen):
        curses.resizeterm(40, 80)
        _width_cases(directory, results)
        _roomy_cases(directory, results)
        _text_cases(directory, results)
        _loop_cases(directory, results)
        _one_cell_cases(results)

    curses.wrapper(execute)
    os.write(int(os.environ["R49_REPORT_FD"]), json.dumps(results).encode())


if __name__ == "__main__":
    main()
