"""R49-D08: bounded room-read incident budget per render."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.secure_store import SecureStoreError
from pomodorough.sentry_monitoring import (
    note_room_read_recovery,
    reset_room_read_incident_budget,
    should_capture_room_read_failure,
)
from pomodorough.ui_views import MainWindowViewMixin


def _infra_secure_error(text: str) -> SecureStoreError:
    error = SecureStoreError(text)
    error.__cause__ = OSError("disk down")
    return error


def _mixin() -> MainWindowViewMixin:
    mixin = MainWindowViewMixin.__new__(MainWindowViewMixin)
    mixin.store = Mock()
    mixin.store.iroh_room.return_value = {"roomId": "room-1"}
    return mixin


class RoomReadBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_room_read_incident_budget()

    def tearDown(self) -> None:
        reset_room_read_incident_budget()

    def test_twenty_denied_reads_emit_one_capture(self) -> None:
        mixin = _mixin()
        self.assertEqual(mixin._cached_iroh_room_value(), {"roomId": "room-1"})
        mixin.store.iroh_room.side_effect = sqlite3.OperationalError("denied")
        with patch("pomodorough.ui_views.capture_exception") as capture:
            for index in range(20):
                mixin.store.iroh_room.side_effect = sqlite3.OperationalError(
                    f"denied {index}"
                )
                self.assertEqual(
                    mixin._cached_iroh_room_value(), {"roomId": "room-1"}
                )
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(mixin.store.iroh_room.call_count, 21)

    def test_changing_text_stays_one_incident(self) -> None:
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("boom A"))
        )
        for index in range(10):
            self.assertFalse(
                should_capture_room_read_failure(
                    sqlite3.OperationalError(f"boom {index}")
                )
            )

    def test_distinct_categories_each_report(self) -> None:
        self.assertTrue(should_capture_room_read_failure(OSError("a")))
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("b"))
        )
        self.assertTrue(
            should_capture_room_read_failure(_infra_secure_error("c"))
        )

    def test_recovery_allows_recurrence(self) -> None:
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("boom"))
        )
        self.assertFalse(
            should_capture_room_read_failure(sqlite3.OperationalError("boom"))
        )
        note_room_read_recovery()
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("boom"))
        )

    def test_success_clears_budget_for_recurrence(self) -> None:
        mixin = _mixin()
        mixin._cached_iroh_room_value()
        mixin.store.iroh_room.side_effect = sqlite3.OperationalError("denied")
        with patch("pomodorough.ui_views.capture_exception") as capture:
            mixin._cached_iroh_room_value()
        self.assertEqual(capture.call_count, 1)
        mixin.store.iroh_room.side_effect = None
        mixin.store.iroh_room.return_value = {"roomId": "room-2"}
        self.assertEqual(mixin._cached_iroh_room_value(), {"roomId": "room-2"})
        mixin.store.iroh_room.side_effect = sqlite3.OperationalError("denied")
        with patch("pomodorough.ui_views.capture_exception") as capture:
            self.assertEqual(mixin._cached_iroh_room_value(), {"roomId": "room-2"})
        self.assertEqual(capture.call_count, 1)

    def test_validation_stays_silent_and_keeps_budget(self) -> None:
        self.assertFalse(should_capture_room_read_failure(ValueError("bad")))
        self.assertFalse(should_capture_room_read_failure(KeyError("bad")))
        self.assertTrue(
            should_capture_room_read_failure(sqlite3.OperationalError("boom"))
        )

    def test_malformed_secure_store_stays_silent(self) -> None:
        mixin = _mixin()
        mixin._cached_iroh_room_value()
        mixin.store.iroh_room.side_effect = SecureStoreError("malformed blob")
        with patch("pomodorough.ui_views.capture_exception") as capture:
            self.assertEqual(mixin._cached_iroh_room_value(), {"roomId": "room-1"})
        capture.assert_not_called()
        mixin.store.iroh_room.side_effect = sqlite3.OperationalError("denied")
        with patch("pomodorough.ui_views.capture_exception") as capture:
            mixin._cached_iroh_room_value()
        self.assertEqual(capture.call_count, 1)

    def test_infra_secure_store_reports_once_per_incident(self) -> None:
        mixin = _mixin()
        mixin._cached_iroh_room_value()
        with patch("pomodorough.ui_views.capture_exception") as capture:
            for index in range(20):
                mixin.store.iroh_room.side_effect = _infra_secure_error(
                    f"infra {index}"
                )
                self.assertEqual(
                    mixin._cached_iroh_room_value(), {"roomId": "room-1"}
                )
        self.assertEqual(capture.call_count, 1)

    def test_retries_and_fallback_rendering_preserved(self) -> None:
        mixin = _mixin()
        mixin.network_screen = Mock()
        mixin.iroh = None
        mixin.replication_mode = "centralized"
        mixin._iroh_status = "off"
        mixin._iroh_details = ""
        mixin._iroh_invite = ""
        mixin.cloud = Mock(authenticated=False, deleting_account=False)
        mixin.account_button = Mock()
        mixin.strings = Mock()
        mixin.strings.text.side_effect = lambda key, **_: key
        mixin._cached_iroh_room_value()
        mixin.store.iroh_room.side_effect = sqlite3.OperationalError("denied")
        with patch("pomodorough.ui_views.capture_exception"):
            mixin._render_network()
        room = mixin.network_screen.render.call_args.kwargs["room"]
        self.assertEqual(room, {"roomId": "room-1"})
        mixin.store.iroh_room.side_effect = None
        mixin.store.iroh_room.return_value = {"roomId": "room-2"}
        with patch("pomodorough.ui_views.capture_exception"):
            mixin._render_network()
        room = mixin.network_screen.render.call_args.kwargs["room"]
        self.assertEqual(room, {"roomId": "room-2"})


class RealEnvelopeTests(unittest.TestCase):
    def test_real_sdk_envelopes_bounded(self) -> None:
        script = textwrap.dedent("""
            import os
            os.environ["POMODOROUGH_SENTRY_ALLOW_IN_TESTS"] = "1"
            import sqlite3
            from unittest.mock import patch
            import sentry_sdk
            from sentry_sdk.transport import Transport
            from pomodorough.sentry_monitoring import (
                init_sentry, reset_room_read_incident_budget,
                should_capture_room_read_failure, note_room_read_recovery,
                capture_exception,
            )
            from pomodorough.secure_store import SecureStoreError

            class MemoryTransport(Transport):
                def __init__(self):
                    super().__init__()
                    self.envelopes = []
                def capture_envelope(self, envelope):
                    self.envelopes.append(envelope.serialize())

            def infra(text):
                error = SecureStoreError(text)
                error.__cause__ = OSError("disk down")
                return error

            transport = MemoryTransport()
            with (
                patch("sentry_sdk.transport.make_transport", return_value=transport),
                patch("sentry_sdk.client.make_transport", return_value=transport),
                patch("socket.socket.connect", side_effect=AssertionError("network")),
            ):
                assert init_sentry(dsn="https://public@example.ingest.sentry.io/1", force=True)
                # 20 denied reads -> 1 envelope.
                reset_room_read_incident_budget()
                base = len(transport.envelopes)
                for i in range(20):
                    error = sqlite3.OperationalError(f"denied {i}")
                    if should_capture_room_read_failure(error):
                        capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base == 1, transport.envelopes

                # Recovery resumes, recurrence budgeted.
                note_room_read_recovery()
                base2 = len(transport.envelopes)
                for i in range(20):
                    error = sqlite3.OperationalError(f"denied again {i}")
                    if should_capture_room_read_failure(error):
                        capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base2 == 1, len(transport.envelopes)

                # Distinct categories each report.
                reset_room_read_incident_budget()
                base3 = len(transport.envelopes)
                for error in (OSError("a"), sqlite3.OperationalError("b"), infra("c")):
                    if should_capture_room_read_failure(error):
                        capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base3 == 3, len(transport.envelopes)

                # Validation silence.
                reset_room_read_incident_budget()
                base4 = len(transport.envelopes)
                assert not should_capture_room_read_failure(ValueError("bad"))
                assert not should_capture_room_read_failure(KeyError("bad"))
                sentry_sdk.flush()
                assert len(transport.envelopes) - base4 == 0

                # Disabled telemetry drops even allowed report.
                sentry_sdk.get_client().close()
                reset_room_read_incident_budget()
                base5 = len(transport.envelopes)
                if should_capture_room_read_failure(OSError("disabled")):
                    capture_exception(OSError("disabled"))
                sentry_sdk.flush()
                assert len(transport.envelopes) - base5 == 0
                sentry_sdk.get_client().close()
            print("real-envelope-ok")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("real-envelope-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
