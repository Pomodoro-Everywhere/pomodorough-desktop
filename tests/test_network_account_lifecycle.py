from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import call, patch

from PySide6.QtWidgets import QApplication

from pomodorough.network import ApiError, CloudService


def _run_immediately(function, on_result, on_error=None) -> None:
    try:
        on_result(function())
    except Exception as error:
        if on_error is None:
            raise
        on_error(error)


class AccountLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_delete_401_refreshes_captured_session_without_persisting(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        cloud.access_token = "expired-access"
        cloud.refresh_token = "captured-refresh"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        refreshed = {"accessToken": "deletion-access"}

        with (
            patch.object(cloud, "_start", side_effect=_run_immediately),
            patch.object(cloud, "stop_revision_stream"),
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch(
                "pomodorough.network._request",
                side_effect=[ApiError("expired", 401), refreshed, {}],
            ) as request,
        ):
            cloud.delete_account("DELETE")

        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "DELETE",
                    "https://example.test/api/v1/account",
                    {"confirmation": "DELETE"},
                    access_token="expired-access",
                ),
                call(
                    "POST",
                    "https://example.test/api/v1/auth/refresh",
                    {"refreshToken": "captured-refresh"},
                ),
                call(
                    "DELETE",
                    "https://example.test/api/v1/account",
                    {"confirmation": "DELETE"},
                    access_token="deletion-access",
                ),
            ],
        )
        save.assert_not_called()
        self.assertFalse(cloud.authenticated)


if __name__ == "__main__":
    unittest.main()
