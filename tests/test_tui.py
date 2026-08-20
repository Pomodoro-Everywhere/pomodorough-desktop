from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from pomodorough import tui
from pomodorough.terminal import InvalidAction


def timer_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "progress": 0.5,
        "phaseLabel": "Focus",
        "remaining": "12:30",
        "status": "running",
        "taskTitle": "",
        "pendingCommands": 0,
        "pendingDurationOperations": 0,
        "pendingAutoStartOperations": 0,
        "historyResolutionPending": False,
    }
    state.update(overrides)
    return state


class FakeScreen:
    def __init__(self, height: int, width: int, keys: list[int] | None = None):
        self.height = height
        self.width = width
        self.keys = list(keys or [])
        self.erases = 0
        self.refreshes = 0
        self.timeout_ms: int | None = None
        self.writes: list[tuple[int, int, str, int, int | None]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.erases += 1

    def addnstr(self, row: int, column: int, text: str, limit: int, *args: int) -> None:
        attribute = args[0] if args else None
        self.writes.append((row, column, text[:limit], limit, attribute))

    def refresh(self) -> None:
        self.refreshes += 1

    def timeout(self, milliseconds: int) -> None:
        self.timeout_ms = milliseconds

    def getch(self) -> int:
        if not self.keys:
            raise AssertionError("fake getch sequence exhausted")
        return self.keys.pop(0)


class TuiTests(unittest.TestCase):
    def test_handle_key_mapping(self) -> None:
        cases = (
            ("space", ord(" "), "primary", ()),
            ("finish", ord("f"), "issue", ("finish",)),
            ("finish uppercase", ord("F"), "issue", ("finish",)),
            ("cancel", ord("x"), "issue", ("cancel",)),
            ("cancel uppercase", ord("X"), "issue", ("cancel",)),
            ("clear", ord("c"), "issue", ("clear",)),
            ("clear uppercase", ord("C"), "issue", ("clear",)),
            ("focus", ord("1"), "select_phase", ("focus",)),
            ("short break", ord("2"), "select_phase", ("short_break",)),
            ("long break", ord("3"), "select_phase", ("long_break",)),
            ("increase", ord("+"), "adjust_duration", (1,)),
            ("increase alternate", ord("="), "adjust_duration", (1,)),
            ("decrease", ord("-"), "adjust_duration", (-1,)),
            ("decrease alternate", ord("_"), "adjust_duration", (-1,)),
        )
        for label, key, method, arguments in cases:
            with self.subTest(label=label):
                timer = Mock()
                self.assertTrue(tui.handle_key(timer, key))
                getattr(timer, method).assert_called_once_with(*arguments)

        for key in (ord("q"), ord("Q")):
            with self.subTest(key=key):
                timer = Mock()
                self.assertFalse(tui.handle_key(timer, key))
                self.assertEqual(timer.mock_calls, [])

        timer = Mock()
        self.assertTrue(tui.handle_key(timer, ord("?")))
        self.assertEqual(timer.mock_calls, [])

    def test_build_lines_includes_optional_state_and_history_details(self) -> None:
        lines = tui.build_lines(
            timer_state(
                taskTitle="Write tests",
                pendingCommands=2,
                pendingDurationOperations=3,
                pendingAutoStartOperations=4,
                historyResolutionPending=True,
            ),
            [
                {
                    "phase": "focus",
                    "taskTitle": "Ship release",
                    "plannedDurationMs": 125_999,
                    "pending": True,
                },
                {"phase": "short_break", "plannedDurationMs": 60_000},
            ],
            80,
            "Cannot pause now",
        )
        rendered = "\n".join(lines)

        self.assertIn("TASK: Write tests", rendered)
        self.assertIn("2 command(s) pending sync", rendered)
        self.assertIn("3 duration preference(s) pending sync", rendered)
        self.assertIn("4 auto-start preference operation(s) pending sync", rendered)
        self.assertIn("Account history resolution pending", rendered)
        self.assertIn("Cannot pause now", rendered)
        self.assertIn("Focus: Ship release", rendered)
        self.assertIn("  2 min *", rendered)
        self.assertIn("Short Break    1 min", rendered)

    def test_build_lines_reports_empty_history(self) -> None:
        rendered = "\n".join(tui.build_lines(timer_state(status="waiting"), [], 80))

        self.assertIn("WAITING", rendered)
        self.assertIn("No arrivals yet", rendered)
        self.assertIn("Your first run appears here.", rendered)
        self.assertNotIn("TASK:", rendered)
        self.assertNotIn("pending sync", rendered)

    def test_draw_warns_on_small_screen(self) -> None:
        screen = FakeScreen(5, 8)
        timer = Mock()
        timer.state.return_value = timer_state()
        timer.completed_history.return_value = []

        tui._draw(screen, timer, "")

        self.assertEqual(screen.erases, 1)
        self.assertEqual(screen.refreshes, 1)
        self.assertEqual(screen.writes, [(0, 0, "Termina", 7, None)])
        timer.state.assert_called_once_with(auto_finish=True)
        timer.completed_history.assert_called_once_with(5)

    def test_draw_renders_normal_screen_and_truncates_writes(self) -> None:
        screen = FakeScreen(14, 40)
        timer = Mock()
        timer.state.return_value = timer_state()
        timer.completed_history.return_value = []

        tui._draw(screen, timer, "")

        self.assertEqual(screen.erases, 1)
        self.assertEqual(screen.refreshes, 1)
        self.assertEqual(screen.writes[0], (0, 0, "POMODOROUGH", 39, tui.curses.A_BOLD))
        self.assertEqual(screen.writes[1][4], tui.curses.A_NORMAL)
        self.assertTrue(all(write[3] == 39 for write in screen.writes))
        self.assertTrue(all(len(write[2]) <= 39 for write in screen.writes))
        self.assertTrue(any("space start/pause/resume" in write[2] for write in screen.writes))

    def test_run_redraws_after_timeout_and_action_errors_then_quits(self) -> None:
        screen = FakeScreen(
            24,
            80,
            [-1, ord(" "), ord("f"), ord("q")],
        )
        timer = Mock()
        timer.primary.side_effect = InvalidAction("not now")
        timer.issue.side_effect = OSError("disk full")
        messages: list[str] = []

        with (
            patch.object(tui.curses, "curs_set") as curs_set,
            patch.object(tui, "_draw", side_effect=lambda _screen, _timer, message: messages.append(message)),
        ):
            tui._run(screen, timer)

        curs_set.assert_called_once_with(0)
        self.assertEqual(screen.timeout_ms, 250)
        self.assertEqual(messages, ["", "", "not now", "disk full"])
        timer.primary.assert_called_once_with()
        timer.issue.assert_called_once_with("finish")
        self.assertEqual(screen.keys, [])

    def test_run_ignores_cursor_visibility_failure(self) -> None:
        screen = FakeScreen(24, 80, [ord("q")])

        with (
            patch.object(tui.curses, "curs_set", side_effect=tui.curses.error("unsupported")),
            patch.object(tui, "_draw") as draw,
        ):
            tui._run(screen, Mock())

        draw.assert_called_once()
        self.assertEqual(screen.timeout_ms, 250)
        self.assertEqual(screen.keys, [])

    def test_main_returns_expected_codes_and_closes_store(self) -> None:
        cases = (
            ("normal", None, 0, ""),
            ("interrupt", KeyboardInterrupt(), 130, ""),
            (
                "terminal error",
                tui.curses.error("broken terminal"),
                2,
                "pomodorough-tui: terminal unavailable: broken terminal\n",
            ),
        )
        data_path = Path("~/pomodorough-test.sqlite3").expanduser()

        for label, failure, expected_code, expected_error in cases:
            with self.subTest(label=label):
                store = Mock()
                timer = Mock()
                stderr = io.StringIO()
                with (
                    patch.object(tui, "Store", return_value=store) as store_type,
                    patch.object(tui, "LocalTimer", return_value=timer) as timer_type,
                    patch.object(tui.curses, "wrapper", side_effect=failure) as wrapper,
                    redirect_stderr(stderr),
                ):
                    result = tui.main(["--data", "~/pomodorough-test.sqlite3"])

                self.assertEqual(result, expected_code)
                self.assertEqual(stderr.getvalue(), expected_error)
                store_type.assert_called_once_with(data_path)
                timer_type.assert_called_once_with(store)
                wrapper.assert_called_once_with(tui._run, timer)
                store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
