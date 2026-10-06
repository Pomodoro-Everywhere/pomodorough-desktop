from __future__ import annotations

import shutil
import sys
from importlib.resources import files

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication


def _sound_path() -> str:
    return str(files("pomodorough.resources").joinpath("completion.wav"))


_LINUX_PLAYERS = ("pw-play", "paplay", "aplay")

_FAILURE_START = "failed_to_start"
_FAILURE_EXIT = "nonzero_exit"
_FAILURE_CRASH = "crashed"
_FAILURE_NO_PLAYER = "no_player"

_NORMAL_EXIT = QProcess.ExitStatus.NormalExit
_CRASH_EXIT = QProcess.ExitStatus.CrashExit
_FAILED_TO_START = QProcess.ProcessError.FailedToStart
_CRASHED = QProcess.ProcessError.Crashed
_UNKNOWN_ERROR = QProcess.ProcessError.UnknownError
_NOT_RUNNING = QProcess.ProcessState.NotRunning


class CompletionSound:
    def __init__(self) -> None:
        self._process: QProcess | None = None
        self._winsound_active = False
        self._remaining_players: list[str] = []
        self._last_failure: str | None = None

    @property
    def is_playing(self) -> bool:
        return self._process is not None or self._winsound_active

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    def _candidate_players(self) -> list[str]:
        if sys.platform.startswith("linux"):
            found: list[str] = []
            for player in _LINUX_PLAYERS:
                executable = shutil.which(player)
                if executable:
                    found.append(executable)
            return found
        if sys.platform == "darwin":
            executable = shutil.which("afplay")
            return [executable] if executable else []
        return []

    def play(self) -> bool:
        if self.is_playing:
            return False
        self.stop()
        self._last_failure = None
        self._remaining_players = []
        sound_path = _sound_path()
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            return self._play_native(sound_path)
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(
                sound_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            self._winsound_active = True
            return True
        self._last_failure = _FAILURE_NO_PLAYER
        QApplication.beep()
        return False

    def _play_native(self, sound_path: str) -> bool:
        candidates = self._candidate_players()
        if not candidates:
            self._last_failure = _FAILURE_NO_PLAYER
            QApplication.beep()
            return False
        for index, executable in enumerate(candidates):
            self._remaining_players = list(candidates[index + 1 :])
            if self._start_process(executable, sound_path):
                return True
        self._remaining_players = []
        self._last_failure = _FAILURE_START
        QApplication.beep()
        return False

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._remaining_players = []
        if process is not None:
            if process.state() != _NOT_RUNNING:
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
        process.finished.connect(lambda *args: self._process_finished(process, *args))
        process.errorOccurred.connect(lambda *args: self._process_error(process, *args))
        process.start(executable, [sound_path])
        if process.waitForStarted(1_000):
            return True
        if self._process is process:
            self._process = None
        process.deleteLater()
        return False

    def _resolve_exit(self, process: QProcess, exit_code, exit_status):
        if exit_code is None:
            try:
                exit_code = process.exitCode()
            except Exception:
                exit_code = 0
        if not isinstance(exit_code, int):
            exit_code = 0
        if exit_status is None:
            try:
                exit_status = process.exitStatus()
            except Exception:
                exit_status = _NORMAL_EXIT
        if exit_status not in (_NORMAL_EXIT, _CRASH_EXIT):
            exit_status = _NORMAL_EXIT
        return exit_code, exit_status

    def _process_finished(self, process: QProcess, exit_code=None, exit_status=None) -> None:
        if self._process is not process:
            return
        code, status = self._resolve_exit(process, exit_code, exit_status)
        if status == _CRASH_EXIT:
            self._fallback_or_beep(process, _FAILURE_CRASH)
            return
        if self._crashed_error(process):
            self._fallback_or_beep(process, _FAILURE_CRASH)
            return
        if code != 0:
            self._fallback_or_beep(process, _FAILURE_EXIT)
            return
        self._process = None
        self._remaining_players = []
        self._last_failure = None
        process.deleteLater()

    def _crashed_error(self, process: QProcess) -> bool:
        try:
            return process.error() == _CRASHED
        except Exception:
            return False

    def _process_error(self, process: QProcess, error=None) -> None:
        if self._process is not process:
            return
        if error is None:
            try:
                error = process.error()
            except Exception:
                error = _UNKNOWN_ERROR
        if error == _FAILED_TO_START:
            self._fallback_or_beep(process, _FAILURE_START)
            return
        self._fallback_or_beep(process, _FAILURE_CRASH)

    def _fallback_or_beep(self, failed_process: QProcess, category: str) -> None:
        remaining = list(self._remaining_players)
        self._process = None
        failed_process.deleteLater()
        sound_path = _sound_path()
        while remaining:
            next_player = remaining.pop(0)
            self._remaining_players = list(remaining)
            if self._start_process(next_player, sound_path):
                return
            category = _FAILURE_START
        self._remaining_players = []
        self._process = None
        self._last_failure = category
        QApplication.beep()


_default_completion_sound = CompletionSound()


def play_completion_sound() -> bool:
    return _default_completion_sound.play()


def stop_completion_sound() -> None:
    _default_completion_sound.stop()
