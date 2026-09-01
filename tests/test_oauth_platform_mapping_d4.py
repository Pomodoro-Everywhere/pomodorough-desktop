import errno
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pomodorough.network import ApiError, CloudService, _desktop_oauth_platform


@pytest.mark.parametrize(
    ("platform", "expected"),
    (("win32", "windows"), ("linux", "linux"), ("darwin", "macos")),
)
def test_desktop_oauth_platform_maps_supported_hosts(
    platform: str,
    expected: str,
) -> None:
    assert _desktop_oauth_platform(platform) == expected


@pytest.mark.parametrize(
    "platform",
    ("WIN32", "Linux", "Darwin", "linux2", "cygwin", "freebsd", ""),
)
def test_desktop_oauth_platform_rejects_aliases_and_unknown_hosts(
    platform: str,
) -> None:
    with pytest.raises(ApiError, match=os.strerror(errno.ENOTSUP)) as raised:
        _desktop_oauth_platform(platform)

    assert raised.value.document == {
        "platform": platform,
        "supportedPlatforms": ["darwin", "linux", "win32"],
    }


@pytest.mark.parametrize(
    ("platform", "expected"),
    (("win32", "windows"), ("linux", "linux"), ("darwin", "macos")),
)
def test_google_exchange_sends_supported_platform_contract(
    platform: str,
    expected: str,
) -> None:
    request = Mock(return_value={"accessToken": "token"})
    service = SimpleNamespace(
        api_base="https://example.test",
        device_id="device-1",
        _request=request,
    )

    with patch("pomodorough.network.sys.platform", platform):
        response = CloudService._exchange_google_identity(
            service,
            "identity-token",
            {"challenge": "native-challenge"},
        )

    assert response == {"accessToken": "token"}
    request.assert_called_once_with(
        "POST",
        "https://example.test/api/v1/auth/google/exchange",
        {
            "idToken": "identity-token",
            "challenge": "native-challenge",
            "deviceId": "device-1",
            "platform": expected,
        },
    )


def test_google_exchange_rejects_unsupported_platform_before_network() -> None:
    request = Mock()
    service = SimpleNamespace(
        api_base="https://example.test",
        device_id="device-1",
        _request=request,
    )

    with (
        patch("pomodorough.network.sys.platform", "plan9"),
        pytest.raises(ApiError, match=os.strerror(errno.ENOTSUP)) as raised,
    ):
        CloudService._exchange_google_identity(
            service,
            "identity-token",
            {"challenge": "native-challenge"},
        )

    assert raised.value.document["platform"] == "plan9"
    request.assert_not_called()
