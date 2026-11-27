import tempfile
import unittest
import uuid
from pathlib import Path

from doggychat.security import (
    TLSIdentity,
    TrustStore,
    load_or_create_local_identity,
    verify_hello_identity,
)


class SecurityTests(unittest.TestCase):
    def test_signed_hello_verifies_identity_and_rejects_tampering(self):
        identity_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            identity = TLSIdentity(
                identity_id,
                Path(directory) / "identity.crt",
                Path(directory) / "identity.key",
            )
            request_id = uuid.uuid4().hex
            member_id = uuid.uuid4().hex
            frame = {
                "id": member_id,
                "request_id": request_id,
                "port": 43210,
                **identity.sign_hello(member_id, request_id, 43210),
            }

            self.assertEqual(
                verify_hello_identity(frame),
                identity.fingerprint,
            )
            frame["port"] += 1
            with self.assertRaisesRegex(ValueError, "signature is invalid"):
                verify_hello_identity(frame)

    def test_tofu_store_persists_first_pin_and_rejects_changes(self):
        identity_id = uuid.uuid4().hex
        second_identity_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trusted.json"
            first = "a" * 64
            changed = "b" * 64
            other = "c" * 64

            store = TrustStore(path)
            another_store = TrustStore(path)
            self.assertEqual(store.trust(identity_id, first), "new")
            self.assertEqual(
                another_store.trust(second_identity_id, other),
                "new",
            )
            self.assertEqual(store.trust(identity_id, first), "known")
            self.assertEqual(store.trust(identity_id, changed), "changed")
            self.assertEqual(
                TrustStore(path)._fingerprints,
                {
                    identity_id: first,
                    second_identity_id: other,
                },
            )

    def test_persistent_local_tls_identity_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            first = load_or_create_local_identity(directory)
            second = load_or_create_local_identity(directory)

            self.assertEqual(second.identity_id, first.identity_id)
            self.assertEqual(second.fingerprint, first.fingerprint)




if __name__ == "__main__":
    unittest.main()
