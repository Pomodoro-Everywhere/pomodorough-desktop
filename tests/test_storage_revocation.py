from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from pomodorough.secure_store import SecureStoreError
from pomodorough.storage_revocation import PendingSessionRevocations
from test_secure_store import MemorySecretStore


def credentials(session: str) -> dict:
    return {
        "accessToken": f"{session}-access",
        "refreshToken": f"{session}-refresh",
        "accessTokenIsFresh": True,
        "acknowledged": False,
    }


class PendingSessionRevocationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secrets = MemorySecretStore({})
        self.pending = PendingSessionRevocations(self.secrets, "device-1")
        self.api = "https://example.test"

    def test_new_adapter_restores_queue_and_acknowledges_only_matching_session(self) -> None:
        self.pending.save(self.api, "first", credentials("first"))
        self.pending.save(self.api, "second", credentials("second"))
        restarted = PendingSessionRevocations(self.secrets, "device-1")
        restarted.acknowledge(self.api, "first")
        restarted.acknowledge(self.api, "absent")
        self.assertEqual(restarted.load(self.api), {"second": credentials("second")})

    def test_api_and_device_scopes_do_not_expose_other_sessions(self) -> None:
        self.pending.save(self.api, "first", credentials("first"))
        other_device = PendingSessionRevocations(self.secrets, "device-2")
        self.assertEqual(other_device.load(self.api), {})
        self.assertEqual(self.pending.load("https://other.test"), {})
        self.assertEqual(self.pending.load(self.api + "/"), {"first": credentials("first")})

    def test_concurrent_session_updates_merge_without_losing_other_obligations(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.pending.save, self.api, str(index), credentials(str(index))) for index in range(20)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.pending.load(self.api)), 20)
        self.pending.save(self.api, "1", credentials("rotated"))
        self.pending.acknowledge(self.api, "2")
        self.assertEqual(len(self.pending.load(self.api)), 19)
        self.assertEqual(self.pending.load(self.api)["1"], credentials("rotated"))

    def test_malformed_queue_fails_closed_without_overwriting_existing_bytes(self) -> None:
        malformed = [b"not json", b"null", b"[]", b'{}', b'\xff',
                     b'{"version":true,"pending":{}}',
                     b'{"version":2,"pending":{}}',
                     b'{"version":1,"pending":[]}',
                     b'{"version":1,"pending":{"id":null}}',
                     b'{"version":1,"pending":{"id":{"accessToken":12}}}']
        missing_access = credentials("stored")
        del missing_access["accessToken"]
        missing_refresh = credentials("stored")
        del missing_refresh["refreshToken"]
        for invalid in (missing_access, missing_refresh):
            malformed.append(json.dumps({"version": 1, "pending": {"id": invalid}}).encode())
        key = self.pending._key(self.api)
        for encoded in malformed:
            with self.subTest(encoded=encoded):
                self.secrets.values[key] = encoded
                with self.assertRaises(SecureStoreError):
                    self.pending.save(self.api, "new", credentials("new"))
                self.assertEqual(self.secrets.values[key], encoded)

    def test_failed_save_does_not_drop_previously_persisted_obligation(self) -> None:
        self.pending.save(self.api, "first", credentials("first"))
        self.secrets.fail_save_key = self.pending._key(self.api)
        with self.assertRaises(OSError):
            self.pending.save(self.api, "second", credentials("second"))
        self.assertEqual(self.pending.load(self.api), {"first": credentials("first")})
        document = json.loads(self.secrets.values[self.pending._key(self.api)])
        self.assertEqual(document["version"], 1)


if __name__ == "__main__":
    unittest.main()
