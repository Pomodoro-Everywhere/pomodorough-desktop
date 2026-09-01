from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pomodorough.storage import Store


@unittest.skipUnless(os.name == "posix", "requires POSIX permissions")
class StoragePermissionTests(unittest.TestCase):
    def test_default_app_created_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            platform_data = Path(root) / "platform-data"
            platform_data.mkdir(mode=0o755)
            app_data = platform_data / "pomodorough"
            database = app_data / "pomodorough.sqlite3"

            with patch("pomodorough.storage.default_data_path", return_value=database):
                store = Store()
            store.close()

            self.assertEqual(stat.S_IMODE(app_data.stat().st_mode), 0o700)

    def test_custom_database_preserves_existing_parent_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            custom_parent = Path(root) / "caller-selected"
            custom_parent.mkdir(mode=0o755)
            custom_parent.chmod(0o755)
            expected_mode = stat.S_IMODE(custom_parent.stat().st_mode)

            store = Store(custom_parent / "custom.sqlite3")
            store.close()

            self.assertEqual(
                stat.S_IMODE(custom_parent.stat().st_mode), expected_mode
            )
