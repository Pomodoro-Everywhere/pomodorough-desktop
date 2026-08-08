from __future__ import annotations

import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .iroh_network import IrohService
from .network import CloudService
from .storage import Store
from .ui import MainWindow, resource_path


def _iroh_service(store: Store) -> IrohService:
    return IrohService(store.path, store.device_id)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Pomodorough")
    app.setApplicationDisplayName("Pomodorough")
    app.setOrganizationName("egigoka")
    app.setDesktopFileName("me.egigoka.Pomodorough")
    icon = QIcon(str(resource_path("icon.svg")))
    app.setWindowIcon(icon)
    app.setQuitOnLastWindowClosed(True)

    store = Store()
    cloud = CloudService(store.device_id)
    iroh = _iroh_service(store)
    window = MainWindow(store, cloud, icon, iroh)
    app.aboutToQuit.connect(cloud.shutdown)
    app.aboutToQuit.connect(iroh.shutdown)
    app.aboutToQuit.connect(store.close)
    window.show()

    screenshot_path = os.environ.get("POMODOROUGH_SCREENSHOT")
    if screenshot_path:
        def capture() -> None:
            window.grab().save(screenshot_path)
            window.quitting = True
            app.quit()

        QTimer.singleShot(1500, capture)

    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    keepalive = QTimer()
    keepalive.start(500)
    keepalive.timeout.connect(lambda: None)
    return app.exec()
