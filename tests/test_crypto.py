"""Signature and key-handling behaviour.

These cover the properties the protocol actually relies on: a signature that covers the
whole request, a job signature that fails the moment anything in the job changes, and a
fingerprint stable enough for a human to compare against a screen.
"""

from __future__ import annotations

import json

import pytest

from qlar_email_gateway import crypto


@pytest.fixture
def key():
    return crypto.generate_private_key()


class TestRequestSignature:
    def test_round_trip(self, key):
        message = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b'{"a":1}')
        signature = crypto.sign(key, message)
        assert crypto.verify(key.public_key(), message, signature) is True

    @pytest.mark.parametrize(
        "method,path,timestamp,nonce,body",
        [
            ("GET", "/jobs/poll", "1700000000", "abc", b'{"a":1}'),
            ("POST", "/jobs/result", "1700000000", "abc", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000001", "abc", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000000", "xyz", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000000", "abc", b'{"a":2}'),
        ],
    )
    def test_every_field_is_covered(self, key, method, path, timestamp, nonce, body):
        # Change any one component and the signature must stop verifying — otherwise that
        # component could be tampered with in transit.
        original = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b'{"a":1}')
        signature = crypto.sign(key, original)

        tampered = crypto.canonical_request(method, path, timestamp, nonce, body)
        assert crypto.verify(key.public_key(), tampered, signature) is False

    def test_another_key_cannot_verify(self, key):
        message = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b"{}")
        signature = crypto.sign(key, message)
        assert crypto.verify(crypto.generate_private_key().public_key(), message, signature) is False

    def test_garbage_signature_returns_false_rather_than_raising(self, key):
        message = crypto.canonical_request("POST", "/x", "1", "n", b"")
        assert crypto.verify(key.public_key(), message, "not-base64!!") is False
        assert crypto.verify(key.public_key(), message, "") is False


EMPTY_HASH = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


def _fixture_job() -> dict:
    """The worked example from PROTOCOL.md §2, shared with Qlar's C# tests."""
    return {
        "jobId": "job_1",
        "type": "send_email",
        "protocol": 1,
        "to": ["Budi@Example.com"],
        "subject": "Re: Harga paket",
        "inReplyTo": "<m1@example.com>",
        "references": ["<root@example.com>", "<m1@example.com>"],
        "textBody": "Halo Budi",
        "htmlBody": "<p>Halo Budi</p>",
        "issuedAt": "2026-10-07T08:00:00Z",
        "expiresAt": "2026-10-07T08:10:00Z",
        "agentId": "agt_1",
        "userId": "budi@example.com",
        "conversationId": "root@example.com",
        "signature": "ignored",
    }


class TestJobSignature:
    def _signed_job(self, key) -> dict:
        job = _fixture_job()
        job["signature"] = crypto.sign(key, crypto.canonical_job_bytes(job))
        return job

    def test_valid_job_verifies(self, key):
        public_pem = crypto.public_key_pem(key)
        assert crypto.verify_job(public_pem, self._signed_job(key)) is True

    def test_altered_recipient_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        # The attack this defends against: a proxy inside the customer's network that
        # terminates TLS and redirects the reply to someone else.
        job["to"] = ["attacker@evil.example"]
        assert crypto.verify_job(public_pem, job) is False

    def test_altered_body_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        job["textBody"] = "Please wire the money to ..."
        assert crypto.verify_job(public_pem, job) is False

    def test_missing_signature_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        del job["signature"]
        assert crypto.verify_job(public_pem, job) is False

    def test_key_order_and_unsigned_extras_do_not_matter(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)

        reordered = json.loads(json.dumps(dict(reversed(list(job.items())))))
        assert crypto.verify_job(public_pem, reordered) is True

        reordered["note"] = "informational only"
        assert crypto.verify_job(public_pem, reordered) is True

    def test_canonical_form_matches_the_documented_field_list(self):
        # Pinned against the exact bytes Qlar's C# builds (Messenger-BE CanonicalBytesTest).
        assert crypto.canonical_job_bytes(_fixture_job()) == (
            b"job_1\nsend_email\n1\nbudi@example.com\nRe: Harga paket\n<m1@example.com>\n"
            b"<root@example.com>,<m1@example.com>\n"
            b"vR+7278oCiP11GZx5vF5k1UCifUxfyC4bS7kmMoP7wI=\nTla78pZSiLIcYnzFn0/c4Z+dC/yOiazDVjBrFnI5/VA=\n"
            b"2026-10-07T08:00:00Z\n2026-10-07T08:10:00Z\nagt_1\nbudi@example.com\nroot@example.com"
        )

    def test_test_connection_job_hashes_empty_bodies(self):
        job = {
            "jobId": "job_2",
            "type": "test_connection",
            "protocol": 1,
            "issuedAt": "2026-10-07T08:00:00Z",
            "expiresAt": "2026-10-07T08:00:45Z",
            "to": None,
            "references": [],
        }
        assert crypto.canonical_job_bytes(job) == (
            b"job_2\ntest_connection\n1\n\n\n\n\n" + EMPTY_HASH.encode() + b"\n" + EMPTY_HASH.encode()
            + b"\n2026-10-07T08:00:00Z\n2026-10-07T08:00:45Z\n\n\n"
        )

    def test_request_signature_matches_dotnet_vector(self):
        body = b'{"messageId":"<m1@example.com>"}'
        assert crypto.canonical_request("POST", "/inbound", "1791360000", "n0nce-fixture", body) == (
            b"POST\n/inbound\n1791360000\nn0nce-fixture\n" + crypto.body_hash(body).encode()
        )

    def test_job_signed_by_python_vector_verifies(self):
        # The same throwaway key and signature Messenger-BE's PythonInteropTest verifies in .NET.
        public_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEndutb0pQEiURWe3HU3jJ4FTU3NaZ\n"
            "e/a76BY9jT7s1E/4NwMSFqU3OXqP7Cs4v0MlvJCi+dUJWQkFJjwmeraRDA==\n"
            "-----END PUBLIC KEY-----\n"
        )
        job = _fixture_job()
        job["signature"] = (
            "MEYCIQCa95Ymf3Abrt4zjbCiAfX2sEma1TH05JuzU5uXLm0vPgIhAMEkk0jFryasYtR1OGXUPO9+9ew8Khle1PVfeSeBxn4/"
        )
        assert crypto.verify_job(public_pem, job) is True


class TestFingerprint:
    def test_is_stable_and_grouped(self, key):
        public_pem = crypto.public_key_pem(key)
        value = crypto.fingerprint(public_pem)

        assert value == crypto.fingerprint(public_pem)
        assert len(value.split(":")) == 32  # SHA-256 as 32 byte-pairs
        assert value == value.upper()

    def test_differs_between_keys(self, key):
        other = crypto.generate_private_key()
        assert crypto.fingerprint(crypto.public_key_pem(key)) != crypto.fingerprint(
            crypto.public_key_pem(other)
        )


class TestKeyFile:
    def test_is_created_once_and_reused(self, tmp_path):
        path = tmp_path / "gateway-key.pem"

        first, created = crypto.load_or_create_private_key(path)
        assert created is True

        second, created_again = crypto.load_or_create_private_key(path)
        assert created_again is False
        # Identity must survive a restart: a new key would mean a new fingerprint and a
        # gateway that silently needs re-approval.
        assert crypto.public_key_pem(first) == crypto.public_key_pem(second)

    def test_is_written_with_owner_only_permissions(self, tmp_path):
        import os
        import stat

        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")

        path = tmp_path / "gateway-key.pem"
        crypto.load_or_create_private_key(path)
        mode = path.stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
