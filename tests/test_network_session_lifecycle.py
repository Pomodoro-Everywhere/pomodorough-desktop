from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from pomodorough.network import ApiError, CloudService


class AuthenticatedSessionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_logout_during_401_refresh_prevents_request_replay(self) -> None:
        cloud = CloudService("device-1", "https://example.test")
        self.addCleanup(cloud.shutdown)
        cloud.authenticated = True
        cloud.access_token = "expired-access"
        cloud.access_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        refreshed = {
            "accessToken": "stale-access",
            "accessTokenExpiresAt": "2099-01-02T03:04:05Z",
            "refreshToken": "stale-refresh",
            "refreshTokenExpiresAt": "2099-02-03T04:05:06Z",
        }
        calls: list[str] = []

        def request(_method: str, url: str, *_args: object, **_kwargs: object):
            calls.append(url)
            if url.endswith("/protected"):
                raise ApiError("expired", 401)
            cloud.logout()
            return refreshed

        with (
            patch.object(cloud.token_store, "load", return_value={"refreshToken": "old"}),
            patch.object(cloud.token_store, "clear"),
            patch.object(cloud.token_store, "save") as save,
            patch.object(cloud, "_start_revocation"),
            patch("pomodorough.network._request", side_effect=request),
            self.assertRaisesRegex(ApiError, "cancelled"),
        ):
            cloud._authorized_request("GET", "/api/v1/protected")

        self.assertEqual(
            calls,
            [
                "https://example.test/api/v1/protected",
                "https://example.test/api/v1/auth/refresh",
            ],
        )
        save.assert_not_called()
        self.assertFalse(cloud.authenticated)


if __name__ == "__main__":
    unittest.main()
