"""Words a failed action for the window from the error type.

Exception text is written for logs: it holds paths, SQL, provider replies and
English validation detail. None of it reaches the user through this module.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from dataclasses import dataclass

from .cloud_request_errors import (
    ServiceUnreachableError,
    SignInRejectedError,
    UnsupportedSignInPlatformError,
)
from .credential_cleanup_errors import (
    AccountDeletedCleanupPendingError,
    CredentialCleanupRetryError,
)
from .iroh_protocol import IrohProtocolError
from .localization import Strings
from .network_account import SignOutCleanupError
from .network_session import ApiError
from .secure_store import (
    SecretServiceUnavailableError,
    SecretToolMissingError,
    SecureStoreError,
    TokenCleanupPendingError,
)
from .shared_core import SharedCoreError, SharedCoreOperationError

# Order matters: a subclass must come before its base. SecureStoreError is an
# OSError, SharedCoreOperationError is a SharedCoreError, and IrohProtocolError
# is a ValueError.
_SENTENCES_BY_ERROR: tuple[tuple[type[Exception], str, bool], ...] = (
    (ServiceUnreachableError, "network.error.offline", True),
    (SignInRejectedError, "network.error.sign_in_rejected", True),
    (UnsupportedSignInPlatformError, "network.error.platform_unsupported", False),
    (AccountDeletedCleanupPendingError, "failure.account_deleted_cleanup_pending", False),
    (SignOutCleanupError, "failure.sign_out_cleanup", False),
    (TokenCleanupPendingError, "failure.credential_cleanup_pending", False),
    (CredentialCleanupRetryError, "failure.credential_cleanup_pending", False),
    # The Linux setup errors keep the install and unlock steps the user needs.
    (SecretToolMissingError, "failure.secret_tool_missing", True),
    (SecretServiceUnavailableError, "failure.secret_service_unavailable", True),
    (SecureStoreError, "failure.secure_store", True),
    (SharedCoreOperationError, "failure.rejected", False),
    (SharedCoreError, "failure.core_unavailable", True),
    (urllib.error.URLError, "network.error.offline", True),
    (ConnectionError, "network.error.offline", True),
    (TimeoutError, "network.error.offline", True),
    (OSError, "failure.storage", True),
    (sqlite3.Error, "failure.storage", True),
    (IrohProtocolError, "network.iroh_operation_failed", True),
    (ValueError, "failure.rejected", False),
    (KeyError, "failure.rejected", False),
    (TypeError, "failure.rejected", False),
)


@dataclass(frozen=True, slots=True)
class FailureNotice:
    sentence: str
    can_retry: bool


def describe_failure(strings: Strings, error: Exception) -> FailureNotice:
    """Word a failure from the error type, never from its text."""
    for error_type, key, can_retry in _SENTENCES_BY_ERROR:
        if isinstance(error, error_type):
            return FailureNotice(strings.text(key), can_retry)
    if isinstance(error, ApiError):
        return FailureNotice(_service_sentence(strings, error), True)
    return FailureNotice(strings.text("failure.generic"), True)


def _service_sentence(strings: Strings, error: ApiError) -> str:
    """Word a failed service request.

    A reply with an HTTP status carries text the server wrote, so only the
    status is shown. Every other ``ApiError`` without a status is raised with
    a catalog sentence as its text; ``tests/test_r2_b3_no_raw_error_text.py``
    holds the raise sites to that.
    """
    if error.status is not None:
        return strings.text("network.error.http", status=error.status)
    return str(error)
