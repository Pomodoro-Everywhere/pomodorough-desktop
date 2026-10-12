"""R48-D07: shared bounded clock incident budget across tick/render."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough.controller_outcomes import Render
from pomodorough.sentry_monitoring import (
    note_clock_recovery,
    reset_clock_incident_budget,
    should_capture_clock_failure,
)
from pomodorough.shared_core import SharedCoreError
from pomodorough.timer_interaction_controller import (
    TimerInteractionContext,
    TimerInteractionController,
    TimerInteractionPorts,
)

DURATIONS = {"focus": 1_500_000, "shortBreak": 300_000, "longBreak": 900_000}


def _running_timer() -> dict[str, object]:
    return {
        "id": "timer-r48d07",
        "phase": "focus",
        "status": "running",
        "plannedDurationMs": DURATIONS["focus"],
        "observedElapsedMs": 0,
        "lastIntent": {"type": "start"},
    }


def _harness() -> SimpleNamespace:
    store = Mock()
    store.has_pending_auto_break.return_value = False
    store.effective_timer_now_ms.return_value = 1_000
    cloud = SimpleNamespace(authenticated=True, busy=False)
    state = SimpleNamespace(timer=_running_timer())
    settings = {
        "selectedPhase": "focus",
        "durationsMs": dict(DURATIONS),
        "selectedTaskId": None,
    }

    def context() -> TimerInteractionContext:
        return TimerInteractionContext(
            store, cloud, False, state.timer, settings, {"id": "u1"},
            [], {}, 0, "offline", False,
        )

    ports = TimerInteractionPorts(
        context=context,
        apply_outcome=Mock(),
        mutation_blocked=Mock(return_value=False),
        issue_command=Mock(),
        maybe_auto_start_break=Mock(return_value=False),
        notice=Mock(),
        task_input_text=Mock(return_value=""),
        clear_task_input=Mock(),
        task_item_data=Mock(return_value=None),
        invalidate_task_selector=Mock(),
        render_task_selector=Mock(),
        refresh_duration_spins=Mock(),
        refresh_auto_breaks=Mock(),
        stop_sound_timer=Mock(),
        stop_completion_sound=Mock(),
        set_stop_sound_control=Mock(),
    )
    return SimpleNamespace(
        store=store, controller=TimerInteractionController(ports),
    )


class SharedBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_clock_incident_budget()

    def tearDown(self) -> None:
        reset_clock_incident_budget()

    def test_tick_and_render_share_one_report(self) -> None:
        harness = _harness()
        harness.store.effective_timer_now_ms.side_effect = sqlite3.OperationalError(
            "db boom"
        )
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as tick_capture:
            harness.controller.tick()
        self.assertEqual(tick_capture.call_count, 1)
        # Render side shares the same global budget: same incident stays
        # suppressed without reaching the SDK.
        self.assertFalse(
            should_capture_clock_failure(sqlite3.OperationalError("db boom"))
        )

    def test_twenty_repeats_stay_bounded_before_sdk(self) -> None:
        harness = _harness()
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            for index in range(20):
                harness.store.effective_timer_now_ms.side_effect = (
                    sqlite3.OperationalError(f"db boom {index}")
                )
                harness.controller.tick()
        # First report only; 19 repeats suppressed before SDK use.
        self.assertEqual(capture.call_count, 1)

    def test_changing_text_stays_one_incident(self) -> None:
        reset_clock_incident_budget()
        self.assertTrue(
            should_capture_clock_failure(sqlite3.OperationalError("boom A"))
        )
        for index in range(10):
            self.assertFalse(
                should_capture_clock_failure(
                    sqlite3.OperationalError(f"boom {index}")
                )
            )

    def test_distinct_categories_each_report(self) -> None:
        reset_clock_incident_budget()
        self.assertTrue(should_capture_clock_failure(OSError("a")))
        self.assertTrue(
            should_capture_clock_failure(sqlite3.OperationalError("b"))
        )
        self.assertTrue(should_capture_clock_failure(SharedCoreError("c")))

    def test_recovery_allows_recurrence(self) -> None:
        reset_clock_incident_budget()
        self.assertTrue(
            should_capture_clock_failure(sqlite3.OperationalError("boom"))
        )
        self.assertFalse(
            should_capture_clock_failure(sqlite3.OperationalError("boom"))
        )
        note_clock_recovery()
        self.assertTrue(
            should_capture_clock_failure(sqlite3.OperationalError("boom"))
        )

    def test_validation_stays_silent_and_keeps_budget(self) -> None:
        reset_clock_incident_budget()
        self.assertFalse(should_capture_clock_failure(ValueError("bad")))
        # Budget untouched: next infra failure still reports.
        self.assertTrue(
            should_capture_clock_failure(sqlite3.OperationalError("boom"))
        )

    def test_tick_preserves_fallback_and_retries(self) -> None:
        harness = _harness()
        harness.store.effective_timer_now_ms.side_effect = OSError("clock boom")
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ):
            outcome = harness.controller.tick()
        # Fallback rendering preserved, no notice, retry next tick.
        self.assertIn(Render, tuple(map(type, outcome.effects)))
        harness.store.effective_timer_now_ms.side_effect = None
        harness.store.effective_timer_now_ms.return_value = 5_000
        with patch(
            "pomodorough.timer_interaction_controller.capture_exception",
        ) as capture:
            outcome = harness.controller.tick()
        capture.assert_not_called()
        self.assertIn(Render, tuple(map(type, outcome.effects)))


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
                init_sentry, reset_clock_incident_budget,
                should_capture_clock_failure, note_clock_recovery,
                capture_exception,
            )
            from pomodorough.shared_core import SharedCoreError

            class MemoryTransport(Transport):
                def __init__(self):
                    super().__init__()
                    self.envelopes = []
                def capture_envelope(self, envelope):
                    self.envelopes.append(envelope.serialize())

            transport = MemoryTransport()
            with (
                patch("sentry_sdk.transport.make_transport", return_value=transport),
                patch("sentry_sdk.client.make_transport", return_value=transport),
                patch("socket.socket.connect", side_effect=AssertionError("network")),
            ):
                assert init_sentry(dsn="https://public@example.ingest.sentry.io/1", force=True)
                # Duplicates: 20 fresh same-type failures -> 1 envelope.
                reset_clock_incident_budget()
                base = len(transport.envelopes)
                for i in range(20):
                    error = sqlite3.OperationalError(f"db boom {i}")
                    if should_capture_clock_failure(error):
                        capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base == 1, transport.envelopes

                # Tick+render sharing: 20 cycles x 2 sites same incident -> 0 new.
                for i in range(20):
                    for _ in range(2):
                        error = sqlite3.OperationalError(f"db boom tick {i}")
                        if should_capture_clock_failure(error):
                            capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base == 1, len(transport.envelopes)

                # Distinct categories each report.
                reset_clock_incident_budget()
                base2 = len(transport.envelopes)
                for error in (OSError("a"), sqlite3.OperationalError("b"), SharedCoreError("c")):
                    if should_capture_clock_failure(error):
                        capture_exception(error)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base2 == 3, len(transport.envelopes)

                # Recovery and recurrence.
                reset_clock_incident_budget()
                base3 = len(transport.envelopes)
                first = sqlite3.OperationalError("boom")
                if should_capture_clock_failure(first):
                    capture_exception(first)
                if should_capture_clock_failure(sqlite3.OperationalError("boom")):
                    capture_exception(sqlite3.OperationalError("boom"))
                note_clock_recovery()
                again = sqlite3.OperationalError("boom")
                if should_capture_clock_failure(again):
                    capture_exception(again)
                sentry_sdk.flush()
                assert len(transport.envelopes) - base3 == 2, len(transport.envelopes)

                # Validation silence.
                reset_clock_incident_budget()
                base4 = len(transport.envelopes)
                assert not should_capture_clock_failure(ValueError("bad"))
                sentry_sdk.flush()
                assert len(transport.envelopes) - base4 == 0

                # Disabled telemetry: closed client drops even allowed report.
                sentry_sdk.get_client().close()
                reset_clock_incident_budget()
                base5 = len(transport.envelopes)
                if should_capture_clock_failure(OSError("disabled")):
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
