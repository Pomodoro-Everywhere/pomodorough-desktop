"""Credential cleanup states that the window words from the type.

The raise sites keep their English text for logs, the CLI and the TUI.
"""

from .secure_store import SecureStoreError, TokenCleanupPendingError


class AccountDeletedCleanupPendingError(TokenCleanupPendingError):
    """The remote account is deleted; local credential cleanup is still queued."""


class CredentialCleanupRetryError(SecureStoreError):
    """A queued credential cleanup failed again and stays queued."""
