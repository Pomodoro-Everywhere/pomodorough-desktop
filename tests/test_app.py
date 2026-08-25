from __future__ import annotations

import os
import signal
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pomodorough import app as app_module


class AppLifecycleTests(unittest.TestCase):
    def test_second_instance_exits_before_opening_shared_store(self) -> None:
        application = MagicMock()
        application.exec.return_value = 17
        lock = MagicMock()
        lock.tryLock.return_value = False

        with (
            patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}, clear=True),
            patch.object(app_module.sys, "argv", ["pomodorough"]),
            patch.object(app_module, "QApplication", return_value=application),
            patch.object(app_module, "QIcon"),
            patch.object(app_module, "_instance_lock", return_value=lock, create=True),
            patch.object(app_module, "Store") as store_type,
            patch.object(app_module, "CloudService"),
            patch.object(app_module, "_iroh_service"),
            patch.object(app_module, "MainWindow"),
            patch.object(app_module, "QTimer"),
            patch.object(app_module.signal, "signal"),
        ):
            result = app_module.main()

        self.assertEqual(result, 0)
        lock.tryLock.assert_called_once_with(0)
        store_type.assert_not_called()
        application.exec.assert_not_called()

    def test_main_wires_application_lifecycle(self) -> None:
        application = MagicMock()
        application.exec.return_value = 17
        lock = MagicMock()
        lock.tryLock.return_value = True
        icon = MagicMock()
        store = MagicMock(device_id="device-42")
        cloud = MagicMock()
        iroh = MagicMock()
        window = MagicMock()
        window.quitting = False
        keepalive = MagicMock()
        icon_path = Path("/bundle/icon.svg")

        with (
            patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}, clear=True),
            patch.object(app_module.sys, "argv", ["pomodorough"]),
            patch.object(app_module, "QApplication", return_value=application) as application_type,
            patch.object(app_module, "QIcon", return_value=icon) as icon_type,
            patch.object(app_module, "_instance_lock", return_value=lock),
            patch.object(app_module, "Store", return_value=store) as store_type,
            patch.object(app_module, "CloudService", return_value=cloud) as cloud_type,
            patch.object(app_module, "_iroh_service", return_value=iroh) as iroh_factory,
            patch.object(app_module, "MainWindow", return_value=window) as window_type,
            patch.object(app_module, "QTimer", return_value=keepalive) as timer_type,
            patch.object(app_module, "resource_path", return_value=icon_path) as resource_path,
            patch.object(app_module.signal, "signal") as install_signal,
        ):
            result = app_module.main()

        application_type.assert_called_once_with(["pomodorough"])
        application.setApplicationName.assert_called_once_with("Pomodorough")
        application.setApplicationDisplayName.assert_called_once_with("Pomodorough")
        application.setOrganizationName.assert_called_once_with("egigoka")
        application.setDesktopFileName.assert_called_once_with("me.egigoka.Pomodorough")
        resource_path.assert_called_once_with("icon.svg")
        icon_type.assert_called_once_with(str(icon_path))
        application.setWindowIcon.assert_called_once_with(icon)
        application.setQuitOnLastWindowClosed.assert_called_once_with(True)
        lock.tryLock.assert_called_once_with(0)
        store_type.assert_called_once_with()
        cloud_type.assert_called_once_with("device-42")
        iroh_factory.assert_called_once_with(store)
        window_type.assert_called_once_with(store, cloud, icon, iroh)
        application.aboutToQuit.connect.assert_has_calls(
            [call(cloud.shutdown), call(iroh.shutdown), call(store.close)]
        )
        window.show.assert_called_once_with()

        install_signal.assert_called_once()
        installed_signal, interrupt_callback = install_signal.call_args.args
        self.assertEqual(installed_signal, signal.SIGINT)
        interrupt_callback(signal.SIGINT, None)
        application.quit.assert_called_once_with()

        timer_type.assert_called_once_with()
        timer_type.singleShot.assert_not_called()
        keepalive.start.assert_called_once_with(500)
        keepalive.timeout.connect.assert_called_once()
        self.assertEqual(result, 17)
        application.exec.assert_called_once_with()

    def test_screenshot_mode_captures_then_quits(self) -> None:
        application = MagicMock()
        application.exec.return_value = 0
        lock = MagicMock()
        lock.tryLock.return_value = True
        icon = MagicMock()
        store = MagicMock(device_id="device-42")
        cloud = MagicMock()
        iroh = MagicMock()
        window = MagicMock()
        keepalive = MagicMock()
        screenshot_path = "/tmp/pomodorough.png"

        with (
            patch.dict(
                os.environ,
                {
                    "QT_QPA_PLATFORM": "offscreen",
                    "POMODOROUGH_SCREENSHOT": screenshot_path,
                },
                clear=True,
            ),
            patch.object(app_module, "QApplication", return_value=application),
            patch.object(app_module, "QIcon", return_value=icon),
            patch.object(app_module, "_instance_lock", return_value=lock),
            patch.object(app_module, "Store", return_value=store),
            patch.object(app_module, "CloudService", return_value=cloud),
            patch.object(app_module, "_iroh_service", return_value=iroh),
            patch.object(app_module, "MainWindow", return_value=window),
            patch.object(app_module, "QTimer", return_value=keepalive) as timer_type,
            patch.object(app_module, "resource_path", return_value=Path("/bundle/icon.svg")),
            patch.object(app_module.signal, "signal"),
        ):
            result = app_module.main()

        timer_type.singleShot.assert_called_once()
        delay, capture = timer_type.singleShot.call_args.args
        self.assertEqual(delay, 1500)

        capture()

        window.grab.assert_called_once_with()
        window.grab.return_value.save.assert_called_once_with(screenshot_path)
        self.assertIs(window.quitting, True)
        application.quit.assert_called_once_with()
        self.assertEqual(result, 0)
        application.exec.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
