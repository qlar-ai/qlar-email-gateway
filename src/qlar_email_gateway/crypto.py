"""Key handling and request/job signatures.

Two directions of trust, both signed:

* **gateway -> Qlar** — every request carries a detached signature over a canonical string
  built from the method, path, timestamp, nonce and a hash of the body. Qlar verifies it
  with the public key the gateway registered at enrolment.
* **Qlar -> gateway** — every job carries a signature over a canonical string built from
  its own named fields, made with Qlar's private key. The gateway verifies it with the
  pinned Qlar public key it received at enrolment.

Signing the *payload* rather than relying on the TLS channel matters here: on-premise
deployments routinely sit behind a TLS-terminating proxy, so "it arrived over HTTPS" says
nothing about who sent it. A bearer token would also be replayable by whoever operates
that proxy; a signature bound to a timestamp and a nonce is not.

Curve choice: **ECDSA P-256 with SHA-256**, signatures in DER (RFC 3279) encoding. P-256
is the one curve available out of the box in every runtime a future port might use —
notably .NET 8, which has no built-in Ed25519. DER is what `cryptography` emits and what
.NET's `DSASignatureFormat.Rfc3279DerSequence` reads.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, EllipticCurvePublicKey

SIGNATURE_HEADER = "X-Qlar-Signature"
GATEWAY_ID_HEADER = "X-Qlar-Gateway-Id"
TIMESTAMP_HEADER = "X-Qlar-Timestamp"
NONCE_HEADER = "X-Qlar-Nonce"
PROTOCOL_HEADER = "X-Qlar-Protocol"


class KeyFilePermissionError(Exception):
    """The private key file is readable by users other than its owner."""


def generate_private_key() -> EllipticCurvePrivateKey:
    """Generates a fresh P-256 private key."""
    return ec.generate_private_key(ec.SECP256R1())


def load_or_create_private_key(path: Path) -> tuple[EllipticCurvePrivateKey, bool]:
    """Loads the gateway's private key, generating and persisting one on first run.

    Returns the key and whether it was newly created. The key is written with mode 0600 —
    it is the gateway's identity, and anyone who can read it can impersonate this
    installation to Qlar.
    """
    if path.exists():
        _assert_private_key_permissions(path)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, EllipticCurvePrivateKey):
            raise ValueError(f"{path} does not contain an EC private key")
        return key, False

    key = generate_private_key()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create the file with 0600 already set rather than writing then chmod-ing: between
    # those two steps the key would briefly be world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    return key, True


def _assert_private_key_permissions(path: Path) -> None:
    """Refuses to use a private key that group or others can read.

    Skipped on Windows, where POSIX mode bits are not meaningful and the check would fire
    on every run for no reason.
    """
    if os.name == "nt":
        return

    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFilePermissionError(
            f"{path} is accessible to group/other (mode {oct(mode & 0o777)}). "
            f"Run: chmod 600 {path}"
        )


def public_key_pem(private_key: EllipticCurvePrivateKey) -> str:
    """Returns the PEM-encoded public key to register with Qlar."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def load_public_key(pem: str) -> EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, EllipticCurvePublicKey):
        raise ValueError("PEM does not contain an EC public key")
    return key


def fingerprint(public_key_pem_text: str) -> str:
    """SHA-256 over the DER SubjectPublicKeyInfo, as grouped uppercase hex.

    This is the string a human compares: it is shown in the Qlar CMS when approving a
    gateway, and printed on-premise by `qlar-email-gateway fingerprint`. Grouping in pairs
    makes reading it aloud or matching it by eye far less error-prone than 64 unbroken
    characters.
    """
    der = load_public_key(public_key_pem_text).public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def new_nonce() -> str:
    """A single-use value that makes a captured request unreplayable."""
    return secrets.token_urlsafe(18)


def body_hash(body: bytes) -> str:
    """base64(SHA-256(body)) — the body's contribution to the canonical string."""
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def canonical_request(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    """Builds the exact byte string both sides sign and verify.

    Newline-separated and field-ordered so that neither side has to agree on a JSON
    serializer. `path` is the request path only (no scheme or host): a customer's reverse
    proxy may rewrite the host, and a signature that broke because of that would be
    impossible to diagnose from the gateway's side.
    """
    parts = [method.upper(), path, timestamp, nonce, body_hash(body)]
    return "\n".join(parts).encode("utf-8")


def sign(private_key: EllipticCurvePrivateKey, message: bytes) -> str:
    """Signs a canonical message, returning base64(DER signature)."""
    signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode("ascii")


def verify(public_key: EllipticCurvePublicKey, message: bytes, signature_b64: str) -> bool:
    """Verifies a base64(DER) signature, returning False rather than raising."""
    try:
        public_key.verify(base64.b64decode(signature_b64), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _text(value: Any) -> str:
    """A scalar as it contributes to the canonical form: null is empty, numbers as integers."""
    if value is None:
        return ""
    if isinstance(value, bool):  # not expected, but must never render as True/False
        return "1" if value else "0"
    return str(value)


def _list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [_text(item) for item in value]


def canonical_job_bytes(job: dict[str, Any]) -> bytes:
    """Canonical form of a job for signature verification (PROTOCOL.md §2, "Signing a job").

    Named fields in a fixed order joined by newlines — deliberately NOT canonical JSON. Two
    runtimes agreeing on "sorted keys, tight separators, no ASCII escaping" sounds simple right
    up until a non-ASCII subject or a `/` turns up and one serializer escapes it differently
    from the other. Every job would then fail verification inside a customer's network, with
    nothing in the logs explaining why. A field list has no such ambiguity.

    ``to`` is lowercased and joined by ``,``; ``references`` is joined by ``,`` as given; the
    two bodies contribute base64(SHA-256) so no newline inside them can shift a field. Everything
    that changes what the gateway will DO is in this list; adding such a field is a protocol
    change.
    """
    parts = [
        _text(job.get("jobId")),
        _text(job.get("type")),
        _text(job.get("protocol")),
        ",".join(address.lower() for address in _list(job.get("to"))),
        _text(job.get("subject")),
        _text(job.get("inReplyTo")),
        ",".join(_list(job.get("references"))),
        body_hash(_text(job.get("textBody")).encode("utf-8")),
        body_hash(_text(job.get("htmlBody")).encode("utf-8")),
        _text(job.get("issuedAt")),
        _text(job.get("expiresAt")),
        _text(job.get("agentId")),
        _text(job.get("userId")),
        _text(job.get("conversationId")),
    ]

    return "\n".join(parts).encode("utf-8")


def verify_job(qlar_public_key_pem: str, job: dict[str, Any]) -> bool:
    """True when the job really came from Qlar and nothing in it was altered."""
    signature = job.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    return verify(load_public_key(qlar_public_key_pem), canonical_job_bytes(job), signature)
