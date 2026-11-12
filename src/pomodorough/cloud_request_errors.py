"""Service request failures whose text is written for diagnostics.

Every other ``ApiError`` without an HTTP status is raised with a catalog
sentence. These three carry a socket reason, Google's reply or a platform
name, so the window words them from the type instead.
"""

from __future__ import annotations

from .network_session import ApiError


class ServiceUnreachableError(ApiError):
    """The request never got a reply; the text holds the transport reason."""


class SignInRejectedError(ApiError):
    """Google's sign-in callback reported an error; the text is Google's."""


class UnsupportedSignInPlatformError(ApiError):
    """Desktop sign-in has no OAuth client for this platform."""
