from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QProcess

from pomodorough.sound import CompletionSound


def _which_for(mapping: dict[str, str | None]):
    def _which(name: str) -> str | None:
        return mapping.get(name)

    return _which


def _finished_of(process: MagicMock):
    return process.finished.connect.call_args.args[0]


def _error_of(process: MagicMock):
    return process.errorOccurred.connect.call_args.args[0]


class R43D09SoundFailureTests(unittest.TestCase):
    def test_nonzero_exit_advances_fallback_then_beeps(self) -> None:
        first = MagicMock()
        first.waitForStarted.return_value = True
        second = MagicMock()
        second.waitForStarted.return_value = True
        player = CompletionSound()
        mapping = {"pw-play": "/usr/bin/pw-play", "paplay": "/usr/bin/paplay", "aplay": None}

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", side_effect=_which_for(mapping)),
            patch("pomodorough.sound.QProcess", side_effect=(first, second)),
            patch("pomodorough.sound.QApplication.beep") as beep,
        ):
            self.assertTrue(player.play())
            _finished_of(first)(1, QProcess.ExitStatus.NormalExit)
            self.assertTrue(player.is_playing)
            self.assertEqual(beep.call_count, 0)
            _finished_of(second)(2, QProcess.ExitStatus.NormalExit)
            self.assertFalse(player.is_playing)

        self.assertEqual(first.deleteLater.call_count, 1)
        self.assertEqual(second.deleteLater.call_count, 1)
        beep.assert_called_once_with()
        self.assertEqual(player.last_failure, "nonzero_exit")

    def test_failed_start_advances_to_next_player(self) -> None:
        first = MagicMock()
        first.waitForStarted.return_value = False
        second = MagicMock()
        second.waitForStarted.return_value = True
        player = CompletionSound()
        mapping = {"pw-play": "/usr/bin/pw-play", "paplay": "/usr/bin/paplay", "aplay": None}

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", side_effect=_which_for(mapping)),
            patch("pomodorough.sound.QProcess", side_effect=(first, second)),
            patch("pomodorough.sound.QApplication.beep") as beep,
        ):
            self.assertTrue(player.play())
            self.assertTrue(player.is_playing)
            _finished_of(second)(0, QProcess.ExitStatus.NormalExit)
            self.assertFalse(player.is_playing)

        beep.assert_not_called()
        self.assertIsNone(player.last_failure)
        self.assertEqual(first.deleteLater.call_count, 1)

    def test_crash_advances_fallback_then_beeps(self) -> None:
        first = MagicMock()
        first.waitForStarted.return_value = True
        second = MagicMock()
        second.waitForStarted.return_value = True
        player = CompletionSound()
        mapping = {"pw-play": "/usr/bin/pw-play", "paplay": "/usr/bin/paplay", "aplay": None}

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", side_effect=_which_for(mapping)),
            patch("pomodorough.sound.QProcess", side_effect=(first, second)),
            patch("pomodorough.sound.QApplication.beep") as beep,
        ):
            self.assertTrue(player.play())
            _error_of(first)(QProcess.ProcessError.Crashed)
            self.assertTrue(player.is_playing)
            _finished_of(second)(0, QProcess.ExitStatus.CrashExit)
            self.assertFalse(player.is_playing)

        beep.assert_called_once_with()
        self.assertEqual(player.last_failure, "crashed")

    def test_normal_completion_reports_no_failure(self) -> None:
        process = MagicMock()
        process.waitForStarted.return_value = True
        process.exitCode.return_value = 0
        process.exitStatus.return_value = QProcess.ExitStatus.NormalExit
        player = CompletionSound()

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", return_value="/usr/bin/pw-play"),
            patch("pomodorough.sound.QProcess", return_value=process),
            patch("pomodorough.sound.QApplication.beep") as beep,
        ):
            self.assertTrue(player.play())
            _finished_of(process)()
            self.assertFalse(player.is_playing)

        beep.assert_not_called()
        self.assertIsNone(player.last_failure)

    def test_explicit_stop_does_not_fallback_or_beep(self) -> None:
        first = MagicMock()
        first.waitForStarted.return_value = True
        second = MagicMock()
        second.waitForStarted.return_value = True
        player = CompletionSound()
        mapping = {"pw-play": "/usr/bin/pw-play", "paplay": "/usr/bin/paplay", "aplay": None}

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", side_effect=_which_for(mapping)),
            patch("pomodorough.sound.QProcess", side_effect=(first, second)) as process_type,
            patch("pomodorough.sound.QApplication.beep") as beep,
        ):
            self.assertTrue(player.play())
            player.stop()
            self.assertFalse(player.is_playing)
            _finished_of(first)(1, QProcess.ExitStatus.NormalExit)
            _error_of(first)(QProcess.ProcessError.Crashed)
            self.assertFalse(player.is_playing)

        self.assertEqual(process_type.call_count, 1)
        beep.assert_not_called()
        self.assertIsNone(player.last_failure)


if __name__ == "__main__":
    unittest.main()
