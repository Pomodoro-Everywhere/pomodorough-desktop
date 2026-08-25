from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from pomodorough.sound import CompletionSound


class CompletionSoundTests(unittest.TestCase):
    def test_linux_completion_sound_owns_pipewire_player(self) -> None:
        process = MagicMock()
        process.waitForStarted.return_value = True
        player = CompletionSound()

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", return_value="/usr/bin/pw-play"),
            patch("pomodorough.sound.QProcess", return_value=process) as process_type,
        ):
            self.assertTrue(player.play())

        process_type.assert_called_once_with()
        program, arguments = process.start.call_args.args
        self.assertEqual(program, "/usr/bin/pw-play")
        self.assertEqual(len(arguments), 1)
        sound_path = Path(arguments[0])
        self.assertEqual(sound_path.name, "completion.wav")
        self.assertTrue(sound_path.is_file())
        self.assertTrue(player.is_playing)

        finished = process.finished.connect.call_args.args[0]
        finished()
        self.assertFalse(player.is_playing)
        process.deleteLater.assert_called_once_with()

    def test_replacement_stops_previous_process_before_starting_next(self) -> None:
        first = MagicMock()
        first.waitForStarted.return_value = True
        first.waitForFinished.return_value = True
        second = MagicMock()
        second.waitForStarted.return_value = True
        second.waitForFinished.return_value = True
        player = CompletionSound()

        with (
            patch("pomodorough.sound.sys.platform", "darwin"),
            patch("pomodorough.sound.shutil.which", return_value="/usr/bin/afplay"),
            patch("pomodorough.sound.QProcess", side_effect=(first, second)),
        ):
            self.assertTrue(player.play())
            self.assertTrue(player.play())
            player.stop()

        first.terminate.assert_called_once_with()
        first.waitForFinished.assert_called_once_with(250)
        first.deleteLater.assert_called_once_with()
        second.terminate.assert_called_once_with()
        second.waitForFinished.assert_called_once_with(250)
        second.deleteLater.assert_called_once_with()
        self.assertFalse(player.is_playing)

    def test_stop_kills_process_that_does_not_terminate(self) -> None:
        process = MagicMock()
        process.waitForStarted.return_value = True
        process.waitForFinished.side_effect = (False, True)
        player = CompletionSound()

        with (
            patch("pomodorough.sound.sys.platform", "linux"),
            patch("pomodorough.sound.shutil.which", return_value="/usr/bin/pw-play"),
            patch("pomodorough.sound.QProcess", return_value=process),
        ):
            self.assertTrue(player.play())
            player.stop()

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.waitForFinished.call_args_list, [call(250), call(250)])
        self.assertFalse(player.is_playing)

    def test_windows_stop_purges_asynchronous_playback(self) -> None:
        winsound = MagicMock()
        winsound.SND_FILENAME = 1
        winsound.SND_ASYNC = 2
        winsound.SND_NODEFAULT = 4
        winsound.SND_PURGE = 8
        player = CompletionSound()

        with (
            patch("pomodorough.sound.sys.platform", "win32"),
            patch.dict(sys.modules, {"winsound": winsound}),
        ):
            self.assertTrue(player.play())
            self.assertTrue(player.is_playing)
            player.stop()

        self.assertEqual(
            winsound.PlaySound.call_args_list,
            [
                call(
                    ANY,
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                ),
                call(None, winsound.SND_PURGE),
            ],
        )
        self.assertFalse(player.is_playing)


if __name__ == "__main__":
    unittest.main()
