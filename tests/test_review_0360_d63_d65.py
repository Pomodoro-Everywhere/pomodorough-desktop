"""0.36.0 review D63-D65: SharedCoreError boundary, TUI close guard, scrub prefix."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.shared_core import SharedCoreError
from pomodorough.terminal import InvalidAction


class Strings:
    @staticmethod
    def text(key: str, **fields: object) -> str:
        return key + (":" + str(fields) if fields else "")

    @staticmethod
    def plural(key: str, count: int) -> str:
        return f"{key}:{count}"


class D63StorageErrorsContractTests(unittest.TestCase):
    def test_cli_and_tui_pin_shared_core_error_as_infra(self) -> None:
        from pomodorough import cli as cli_module
        from pomodorough import tui as tui_module

        self.assertIn(SharedCoreError, cli_module.STORAGE_ERRORS)
        self.assertIn(SharedCoreError, tui_module.STORAGE_ERRORS)

    def test_cli_run_with_store_captures_shared_core_error(self) -> None:
        from pomodorough import cli as cli_module

        error = SharedCoreError("wasm load failed")
        with patch.object(cli_module, "LocalTimer"):
            with patch.object(cli_module, "run", side_effect=error):
                with patch(
                    "pomodorough.cli.capture_exception",
                ) as capture:
                    result = cli_module._run_with_store(
                        Mock(), Mock(), Mock(), Strings(),
                    )
        self.assertIs(result, error)
        capture.assert_called_once_with(error)

    def test_cli_main_start_never_escapes_wasm_failure(self) -> None:
        from pomodorough import cli as cli_module

        error = SharedCoreError("wasm load failed")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli_module, "Store", side_effect=error):
            with patch(
                "pomodorough.cli.capture_exception",
            ) as capture:
                try:
                    result = cli_module.main(
                        ("start", "--json"), stdout=stdout, stderr=stderr,
                    )
                except SharedCoreError:
                    self.fail("SharedCoreError escaped cli.main")
        self.assertEqual(result, 2)
        capture.assert_called_once_with(error)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["code"], "storage_error")
        self.assertEqual(stdout.getvalue(), "")

    def test_tui_startup_captures_shared_core_error(self) -> None:
        from pomodorough import tui as tui_module

        error = SharedCoreError("wasm load failed")
        with (
            patch.object(tui_module, "Store", side_effect=error),
            patch.object(tui_module, "LocalTimer"),
            patch.object(tui_module.curses, "wrapper"),
            patch("pomodorough.tui.capture_exception") as capture,
        ):
            try:
                result = tui_module.main([])
            except SharedCoreError:
                self.fail("SharedCoreError escaped tui.main")
        self.assertEqual(result, 2)
        capture.assert_called_once_with(error)

    def test_tui_loop_captures_shared_core_error_and_survives(self) -> None:
        from pomodorough import tui as tui_module

        from tests.test_tui import FakeScreen

        error = SharedCoreError("wasm load failed")
        screen = FakeScreen(24, 80, [ord(" "), ord("q")])
        timer = Mock()
        timer.primary.side_effect = error
        messages: list[str] = []
        with (
            patch.object(tui_module.curses, "curs_set"),
            patch.object(
                tui_module,
                "_draw",
                side_effect=lambda _s, _t, message: messages.append(message),
            ),
            patch("pomodorough.tui.capture_exception") as capture,
        ):
            tui_module._run(screen, timer)
        capture.assert_called_once_with(error)
        self.assertEqual(messages, ["", "wasm load failed"])
        self.assertEqual(screen.keys, [])

    def test_app_startup_captures_shared_core_error_once(self) -> None:
        from pomodorough import app as app_module

        error = SharedCoreError("wasm load failed")
        lock = Mock()
        lock.tryLock.return_value = True
        with (
            patch.object(
                app_module, "init_sentry_from_environment", return_value=False
            ),
            patch.object(app_module, "QApplication", return_value=Mock()),
            patch.object(app_module, "QIcon"),
            patch.object(app_module, "_instance_lock", return_value=lock),
            patch.object(app_module, "Store", side_effect=error),
            patch.object(app_module, "CloudService"),
            patch.object(app_module, "_iroh_service"),
            patch.object(app_module, "MainWindow"),
            patch.object(app_module, "capture_exception") as app_capture,
            patch(
                "pomodorough.sentry_monitoring.capture_exception",
            ) as sentry_capture,
        ):
            try:
                result = app_module.main()
            except SharedCoreError:
                self.fail("SharedCoreError escaped app.main")
        self.assertEqual(result, 1)
        app_capture.assert_called_once_with(error)
        sentry_capture.assert_not_called()


class D64TuiCloseGuardTests(unittest.TestCase):
    def _main_with_close_failure(
        self, body_error: BaseException | None, close_error: BaseException
    ) -> tuple[int, str, Mock]:
        from pomodorough import tui as tui_module

        store = Mock()
        store.close.side_effect = close_error
        wrapper_effect = body_error
        stderr = io.StringIO()
        with (
            patch.object(tui_module, "Store", return_value=store),
            patch.object(tui_module, "LocalTimer", return_value=Mock()),
            patch.object(
                tui_module.curses, "wrapper", side_effect=wrapper_effect
            ),
            patch("pomodorough.tui.capture_exception") as capture,
            redirect_stderr(stderr),
        ):
            try:
                result = tui_module.main([])
            except Exception as exc:  # noqa: BLE001 - guard must not raise
                self.fail(f"close failure escaped tui.main: {exc!r}")
        return result, stderr.getvalue(), capture

    def test_close_infra_never_masks_validation_error(self) -> None:
        body = InvalidAction("cannot pause now")
        close = sqlite3.OperationalError("close failed")
        result, stderr, capture = self._main_with_close_failure(body, close)
        self.assertEqual(result, 2)
        self.assertIn("cannot pause now", stderr)
        self.assertNotIn("close failed", stderr)
        capture.assert_called_once_with(close)

    def test_close_infra_preserves_first_infra_error(self) -> None:
        body = OSError("disk gone")
        close = sqlite3.OperationalError("close failed")
        result, stderr, capture = self._main_with_close_failure(body, close)
        self.assertEqual(result, 2)
        self.assertIn("disk gone", stderr)
        self.assertNotIn("close failed", stderr)
        self.assertEqual(capture.call_count, 2)

    def test_close_infra_on_success_becomes_2_with_capture(self) -> None:
        close = sqlite3.OperationalError("close failed")
        result, stderr, capture = self._main_with_close_failure(None, close)
        self.assertEqual(result, 2)
        self.assertIn("close failed", stderr)
        capture.assert_called_once_with(close)

    def test_close_shared_core_error_captured_on_success(self) -> None:
        close = SharedCoreError("wasm teardown failed")
        result, stderr, capture = self._main_with_close_failure(None, close)
        self.assertEqual(result, 2)
        self.assertIn("wasm teardown failed", stderr)
        capture.assert_called_once_with(close)


class D65SemicolonScrubTests(unittest.TestCase):
    def test_question_mark_still_filtered(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        rendered = json.dumps(
            scrub_sentry_event({"message": "GET https://x/cb?roomSecret=ABC"}, None)
        )
        self.assertNotIn("ABC", rendered)
        self.assertIn("[Filtered]", rendered)

    def test_semicolon_joined_param_filtered(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        urls = (
            "https://x/cb?next=1;roomSecret=ABC",
            "https://x/cb;roomSecret=ABC;next=1",
            "https://x/cb?roomSecret=ABC;next=1",
        )
        for url in urls:
            with self.subTest(url=url):
                rendered = json.dumps(scrub_sentry_event({"message": url}, None))
                self.assertNotIn("ABC", rendered)
                self.assertIn("[Filtered]", rendered)

    def test_semicolon_code_param_filtered(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        rendered = json.dumps(
            scrub_sentry_event({"message": "GET https://x/cb;code=SECRET"}, None)
        )
        self.assertNotIn("SECRET", rendered)
        self.assertIn("[Filtered]", rendered)

    def test_bare_kv_prose_is_documented_limitation(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        # D65: bare `k=v` prose without a ?&#; prefix cannot be scrubbed
        # without over-filtering benign prose (`room=5`); pin passthrough.
        rendered = json.dumps(
            scrub_sentry_event({"message": "note roomSecret=ABC done"}, None)
        )
        self.assertIn("ABC", rendered)

    def test_benign_semicolon_params_stay(self) -> None:
        from pomodorough.sentry_monitoring import scrub_sentry_event

        rendered = json.dumps(
            scrub_sentry_event({"message": "GET https://x/cb?next=1;page=2"}, None)
        )
        self.assertIn("next=1", rendered)
        self.assertIn("page=2", rendered)


if __name__ == "__main__":
    unittest.main()
