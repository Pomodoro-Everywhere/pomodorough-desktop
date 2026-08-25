from __future__ import annotations

import shutil
import sys
from importlib.resources import files

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication


def _sound_path() -> str:
    return str(files("pomodorough.resources").joinpath("completion.wav"))


class CompletionSound:
    def __init__(self) -> None:
        self._process: QProcess | None = None
        self._winsound_active = False

    @property
    def is_playing(self) -> bool:
        return self._process is not None or self._winsound_active

    def play(self) -> bool:
        self.stop()
        sound_path = _sound_path()
        if sys.platform.startswith("linux"):
            for player in ("pw-play", "paplay", "aplay"):
                executable = shutil.which(player)
                if executable and self._start_process(executable, sound_path):
                    return True
        elif sys.platform == "darwin":
            executable = shutil.which("afplay")
            if executable and self._start_process(executable, sound_path):
                return True
        elif sys.platform == "win32":
            import winsound

            winsound.PlaySound(
                sound_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            self._winsound_active = True
            return True

        QApplication.beep()
        return False

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(250):
                    process.kill()
                    process.waitForFinished(250)
            process.deleteLater()

        if self._winsound_active:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
            self._winsound_active = False

    def _start_process(self, executable: str, sound_path: str) -> bool:
        process = QProcess()
        self._process = process
        process.finished.connect(lambda *_args: self._process_finished(process))
        process.start(executable, [sound_path])
        if process.waitForStarted(1_000):
            return True
        self._process = None
        process.deleteLater()
        return False

    def _process_finished(self, process: QProcess) -> None:
        if self._process is not process:
            return
        self._process = None
        process.deleteLater()


_default_completion_sound = CompletionSound()


def play_completion_sound() -> bool:
    return _default_completion_sound.play()


def stop_completion_sound() -> None:
    _default_completion_sound.stop()
