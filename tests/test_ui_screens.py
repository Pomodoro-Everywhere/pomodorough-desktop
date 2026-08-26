from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from pomodorough.arrivals_screen import ArrivalsScreen
from pomodorough.localization import Strings
from pomodorough.network_screen import NetworkScreen
from pomodorough.tasks_screen import TasksScreen
from pomodorough.timer_screen import TimerScreen
from pomodorough.ui_controller import WindowApplicationController
from pomodorough.ui_views import ScreenNavigation


class ScreenSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.strings = Strings("en")

    def test_navigation_emits_page_without_application_side_effects(self) -> None:
        navigation = ScreenNavigation(self.strings)
        requested: list[int] = []
        navigation.screen_requested.connect(requested.append)

        navigation.buttons[2].click()

        self.assertEqual(requested, [2])
        self.assertTrue(navigation.buttons[2].isChecked())

    def test_controller_orders_navigation_render_before_optional_sync(self) -> None:
        class Controller(WindowApplicationController):
            replication_mode = "centralized"

            def __init__(self) -> None:
                self.events: list[tuple[str, int | None]] = []

            def _display_screen(self, index: int) -> None:
                self.events.append(("display", index))

            def _sync(self) -> None:
                self.events.append(("sync", None))

        controller = Controller()

        controller._show_screen(2)
        controller._show_screen(0)
        controller._show_screen(3, sync=False)

        self.assertEqual(
            controller.events,
            [
                ("display", 2),
                ("sync", None),
                ("display", 0),
                ("display", 3),
            ],
        )

    def test_timer_controls_emit_typed_actions_in_click_order(self) -> None:
        screen = TimerScreen(
            self.strings,
            {
                "durations": {
                    "focus": 25,
                    "short_break": 5,
                    "long_break": 15,
                },
                "autoStartBreaks": False,
            },
        )
        actions: list[tuple[str, object]] = []
        screen.primary_action_requested.connect(
            lambda: actions.append(("primary", None))
        )
        screen.command_requested.connect(
            lambda command: actions.append(("command", command))
        )
        screen.stop_sound_requested.connect(
            lambda: actions.append(("stop_sound", None))
        )

        screen.primary_button.click()
        screen.finish_button.click()
        screen.cancel_button.click()
        screen.stop_sound_button.setVisible(True)
        screen.stop_sound_button.click()

        self.assertEqual(
            actions,
            [
                ("primary", None),
                ("command", "finish"),
                ("command", "cancel"),
                ("stop_sound", None),
            ],
        )

    def test_timer_settings_emit_phase_duration_task_and_auto_break_values(
        self,
    ) -> None:
        screen = TimerScreen(
            self.strings,
            {
                "durations": {
                    "focus": 25,
                    "short_break": 5,
                    "long_break": 15,
                },
                "autoStartBreaks": False,
            },
        )
        events: list[tuple[object, ...]] = []
        screen.phase_selected.connect(lambda phase: events.append(("phase", phase)))
        screen.duration_changed.connect(
            lambda phase, value: events.append(("duration", phase, value))
        )
        screen.task_selection_changed.connect(
            lambda index: events.append(("task", index))
        )
        screen.auto_breaks_changed.connect(
            lambda enabled: events.append(("auto", enabled))
        )

        screen.phase_buttons["short_break"].click()
        screen.duration_spins["focus"].setValue(26)
        screen.task_combo.blockSignals(True)
        screen.task_combo.addItem(self.strings.text("task.unassigned"), None)
        screen.task_combo.addItem("Write tests", "task-1")
        screen.task_combo.blockSignals(False)
        screen.task_combo.setCurrentIndex(1)
        screen.auto_breaks.setChecked(True)

        self.assertEqual(
            events,
            [
                ("phase", "short_break"),
                ("duration", "focus", 26),
                ("task", 1),
                ("auto", True),
            ],
        )

    def test_tasks_screen_renders_and_emits_task_payloads(self) -> None:
        screen = TasksScreen(self.strings)
        events: list[tuple[str, str]] = []
        screen.add_task_requested.connect(lambda title: events.append(("add", title)))
        screen.delete_task_requested.connect(
            lambda task_id: events.append(("delete", task_id))
        )
        screen.task_input.setText("Architecture")

        screen.add_task_button.click()
        screen.render(
            [{"id": "task-1", "title": "Architecture"}],
            [],
            mutations_enabled=True,
        )
        delete = screen.task_table.cellWidget(0, 3)
        self.assertIsInstance(delete, QPushButton)
        delete.click()

        self.assertEqual(
            events,
            [("add", "Architecture"), ("delete", "task-1")],
        )
        self.assertEqual(screen.task_table.item(0, 0).text(), "Architecture")

    def test_arrivals_screen_owns_terminal_history_rendering(self) -> None:
        screen = ArrivalsScreen(self.strings, "device-12345678")
        screen.render(
            [
                {
                    "id": "history-1",
                    "taskId": "task-1",
                    "phase": "focus",
                    "status": "completed",
                    "plannedDurationMs": 1_500_000,
                    "completedAt": "2026-01-01T12:00:00Z",
                }
            ],
            {"task-1": {"id": "task-1", "title": "Architecture"}},
        )

        self.assertEqual(screen.history_list.count(), 1)
        self.assertIn("Architecture", screen.history_list.item(0).text())
        self.assertIn("1", screen.history_count.text())

    def test_network_screen_emits_route_and_room_payloads(self) -> None:
        screen = NetworkScreen(self.strings, "offline")
        events: list[tuple[str, object]] = []
        screen.replication_mode_requested.connect(
            lambda mode: events.append(("mode", mode))
        )
        screen.create_room_requested.connect(
            lambda name: events.append(("create", name))
        )
        screen.join_room_requested.connect(
            lambda invite: events.append(("join", invite))
        )

        screen.replication_mode_combo.setCurrentIndex(
            screen.replication_mode_combo.findData("iroh")
        )
        screen.room_name_input.setText("Shared room")
        screen.create_room_button.click()
        screen.invite_input.setPlainText("iroh-ticket")
        screen.join_room_button.click()

        self.assertEqual(
            events,
            [
                ("mode", "iroh"),
                ("create", "Shared room"),
                ("join", "iroh-ticket"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
