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

    def test_delete_401_validates_complete_pair_and_persists_before_delete(self) -> None:
        complete = {
            "accessToken": "deletion-access", "refreshToken": "rotated-refresh",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        for refreshed in ({"accessToken": "deletion-access"}, complete):
            cloud = CloudService("device-1", "https://example.test")
            self.addCleanup(cloud.shutdown)
            cloud.authenticated = True
            cloud.access_token, cloud.refresh_token = "expired-access", "captured-refresh"
            cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

            def request(method, _url, _payload, **kwargs):
                if method == "POST":
                    return refreshed
                if kwargs["access_token"] == "expired-access":
                    raise ApiError("expired", 401)
                save.assert_called_once_with(complete)
                self.assertEqual(cloud.refresh_token, "rotated-refresh")
                return {}

            with (
                patch.object(cloud, "_start", side_effect=_run_immediately),
                patch.object(cloud, "stop_revision_stream"),
                patch.object(cloud, "start_revision_stream"),
                patch.object(cloud.token_store, "clear"),
                patch.object(cloud.token_store, "save") as save,
                patch("pomodorough.network._request", side_effect=request) as transport,
                self.subTest(complete=refreshed is complete),
            ):
                cloud.delete_account("DELETE")
            expected = [
                call("DELETE", "https://example.test/api/v1/account", {"confirmation": "DELETE"}, access_token="expired-access"),
                call("POST", "https://example.test/api/v1/auth/refresh", {"refreshToken": "captured-refresh"}),
            ]
            if refreshed is complete:
                expected.append(call("DELETE", "https://example.test/api/v1/account", {"confirmation": "DELETE"}, access_token="deletion-access"))
                save.assert_called_once_with(complete)
                self.assertFalse(cloud.authenticated)
            else:
                save.assert_not_called()
                self.assertTrue(cloud.authenticated)
                self.assertEqual(cloud.refresh_token, "captured-refresh")
            self.assertEqual(transport.call_args_list, expected)


if __name__ == "__main__":
    unittest.main()
