from __future__ import annotations

import unittest

from pomodorough.network import CloudService


class _ReplacementSession:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_access(self, generation: int | None = None) -> str:
        self.calls += 1
        return f"replacement:{generation}"


class CloudServiceArchitectureTests(unittest.TestCase):
    def test_compatibility_state_attributes_are_not_mirrored_properties(self) -> None:
        names = (
            "access_token",
            "refresh_token",
            "access_expires_at",
            "authenticated",
            "busy",
            "deleting_account",
            "_sync_queued",
            "_account_generation",
            "_shutting_down",
            "_lifecycle_lock",
            "_network",
            "_revision_reply",
            "_revision_parser",
            "_revision_reconnect",
            "_revision_reconnect_attempt",
        )

        self.assertFalse(
            any(isinstance(CloudService.__dict__[name], property) for name in names)
        )

    def test_compatibility_methods_call_the_current_session_collaborator(self) -> None:
        cloud = CloudService("device-architecture", "https://example.test")
        self.addCleanup(cloud.shutdown)
        replacement = _ReplacementSession()
        cloud._session = replacement  # type: ignore[assignment]

        result = cloud._ensure_access(7)

        self.assertEqual(result, "replacement:7")
        self.assertEqual(replacement.calls, 1)
        self.assertNotIn("_ensure_access", cloud.__dict__)

    def test_bound_account_method_aliases_are_not_stored_on_the_facade(self) -> None:
        cloud = CloudService("device-architecture", "https://example.test")
        self.addCleanup(cloud.shutdown)

        for name in (
            "_accept_tokens",
            "_accept_login_tokens",
            "_authorized_request",
            "_timed_request",
            "_begin_account_deletion",
            "_delete_captured_account",
            "_refresh_deletion_access",
            "_revoke_credentials",
            "_refresh_revocation_access",
        ):
            self.assertNotIn(name, cloud.__dict__)


if __name__ == "__main__":
    unittest.main()
