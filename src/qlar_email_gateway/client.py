"""Signed HTTP transport to Qlar.

Every call is a plain HTTPS POST with four extra headers: who is calling, when, a nonce,
and a signature over a canonical string built from all of it plus a hash of the body.

One subtlety worth stating, because getting it wrong produces signature failures that are
miserable to debug: **the signed path is the endpoint path relative to the configured base
URL** (`/jobs/poll`), not the absolute path of the request (`/api/email-gateway/jobs/poll`).
Qlar sits behind an API gateway that may rewrite the prefix, and a signature that broke
because of a gateway rule would look, from inside the customer's network, exactly like a
wrong key.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from . import PROTOCOL_VERSION
from .crypto import (
    GATEWAY_ID_HEADER,
    NONCE_HEADER,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    canonical_request,
    new_nonce,
    sign,
)


class Revoked(Exception):
    """Qlar says this gateway is revoked. Both loops stop; a human must re-enrol it."""


class Deleted(Revoked):
    """Qlar has no gateway with this id any more: it was deleted in the CMS.

    A subclass because every loop must stop exactly as it does for a revocation. The difference
    is what happens next: a revoked gateway still exists and can be inspected in the CMS, while a
    deleted one never will again, so its state file is dead weight that would only block the next
    enrolment.
    """


def stop_reason(rejection: QlarRejected) -> type[Revoked] | None:
    """The exception a refusal means the gateway must stop with, or None to carry on."""
    if rejection.status != 403:
        return None
    reason = str(rejection.body.get("reason", "")).lower()
    if reason == "unknown_gateway":
        return Deleted
    if reason == "revoked":
        return Revoked
    return None


class QlarUnreachable(Exception):
    """Qlar could not be reached — a network problem, not a rejection."""


class QlarRejected(Exception):
    """Qlar answered with a refusal. `status` carries the HTTP code."""

    def __init__(self, status: int, message: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body or {}


class QlarResponseTooLarge(QlarRejected):
    """A download was larger than the caller allowed; it was abandoned, not read to the end."""


class QlarNotAnEndpoint(QlarRejected):
    """Something answered, but it was not Qlar's API.

    A wrong `QLAR_BASE_URL` does not fail like a wrong password: a static site, a CDN, a
    captive portal or a reverse proxy all answer cheerfully with an HTML error page, and a
    404 in particular is indistinguishable from a legitimate "no such enrolment code"
    unless the body is examined. Raised as a subclass so that every existing handler still
    catches it, while callers that can give better advice may look for it first.
    """

    def __init__(self, status: int, url: str, content_type: str) -> None:
        super().__init__(status, f"{url} answered HTTP {status} as {content_type or 'an empty body'}")
        self.url = url
        self.content_type = content_type


@dataclass
class QlarClient:
    base_url: str
    private_key: EllipticCurvePrivateKey
    gateway_id: str | None
    verify_tls: bool = True

    def post(
        self, path: str, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> tuple[int, dict[str, Any]]:
        """POSTs a signed JSON request, returning the status and decoded body.

        Raises QlarUnreachable for transport failures and QlarRejected for 4xx/5xx, so a
        caller can tell "the network is down" from "Qlar says no" — they need very
        different handling, and only one of them should stop the gateway.
        """
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        status, decoded, _ = self._exchange("POST", path, body, "application/json", timeout)
        return status, decoded

    def post_bytes(
        self, path: str, body: bytes, content_type: str, *, timeout: float = 60.0
    ) -> tuple[int, dict[str, Any]]:
        """POSTs a signed raw body (an attachment). `path` may carry a query string.

        The signature covers the path *with* its query exactly as sent, so the caller must
        percent-encode the query before calling and nothing here re-encodes or reorders it.
        Errors are raised as in `post`.
        """
        status, decoded, _ = self._exchange("POST", path, body, content_type, timeout)
        return status, decoded

    def get_bytes(self, path: str, *, max_bytes: int, timeout: float = 30.0) -> bytes:
        """GETs a signed, empty-bodied request and returns the raw response bytes.

        The signed path is relative to the API root, like every other call, and the body hash is
        the hash of the empty body. A fresh nonce is used on every call. Errors are raised as in
        `post`. The body is streamed and never decoded: a declared `Content-Length` over
        `max_bytes` is refused before reading, and the download is abandoned the moment the bytes
        received exceed it (QlarResponseTooLarge).
        """
        _, _, content = self._exchange("GET", path, b"", None, timeout, max_bytes=max_bytes)
        return content

    def _exchange(
        self,
        method: str,
        path: str,
        body: bytes,
        content_type: str | None,
        timeout: float,
        max_bytes: int | None = None,
    ) -> tuple[int, dict[str, Any], bytes]:
        timestamp = str(int(time.time()))
        nonce = new_nonce()

        message = canonical_request(method, path, timestamp, nonce, body)
        headers = {
            PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: sign(self.private_key, message),
        }
        if content_type:
            headers["Content-Type"] = content_type
        if self.gateway_id:
            headers[GATEWAY_ID_HEADER] = self.gateway_id

        url = f"{self.base_url.rstrip('/')}{path}"

        try:
            with (
                httpx.Client(verify=self.verify_tls, timeout=timeout, follow_redirects=False) as client,
                client.stream(method, url, content=body or None, headers=headers) as response,
            ):
                failed = response.status_code >= 400
                binary = max_bytes is not None and not failed
                if binary:
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > (max_bytes or 0):
                        raise QlarResponseTooLarge(response.status_code, "response larger than allowed")
                limit = max_bytes if binary else ERROR_BODY_LIMIT if failed else JSON_BODY_LIMIT
                content = _read_capped(response, limit or 0, strict=binary)
        except httpx.HTTPError as error:
            raise QlarUnreachable(str(error)) from error

        decoded: dict[str, Any] = {}
        if content and not binary:
            try:
                parsed = json.loads(content)
                decoded = parsed if isinstance(parsed, dict) else {"value": parsed}
            except ValueError:
                decoded = {"raw": content[:500].decode("utf-8", errors="replace")}

        if failed:
            if _is_not_our_api(response, decoded):
                raise QlarNotAnEndpoint(response.status_code, url, response.headers.get("content-type", ""))
            raise QlarRejected(
                response.status_code,
                str(decoded.get("message") or decoded.get("title") or response.reason_phrase),
                decoded,
            )

        return response.status_code, decoded, content


#: How much of an error answer is read; enough for Qlar's small JSON refusals, never a download.
ERROR_BODY_LIMIT = 4096
#: Qlar's JSON answers are small; this only stops a misbehaving server from filling memory.
JSON_BODY_LIMIT = 4 * 1024 * 1024


def _read_capped(response: httpx.Response, limit: int, *, strict: bool) -> bytes:
    """Reads at most `limit` bytes. Over the limit: raise when `strict`, else truncate."""
    received = bytearray()
    for chunk in response.iter_bytes():
        received.extend(chunk)
        if len(received) > limit:
            if strict:
                raise QlarResponseTooLarge(response.status_code, "response larger than allowed")
            return bytes(received[:limit])
    return bytes(received)


def _is_not_our_api(response: httpx.Response, decoded: dict[str, Any]) -> bool:
    """Decides whether a failing response came from Qlar's API at all.

    Deliberately narrow, because the cost of a false positive is telling someone their URL
    is wrong when Qlar really did refuse them:

    * **HTML, at any status.** Our API never answers a signed POST with a web page. A
      static site, a login portal or a proxy error page does.
    * **A 404 with nothing JSON in it.** Either the path is wrong or the endpoint is not
      deployed at that host; both are the operator's URL, not their enrolment code.

    Anything else — a JSON 400, an empty 500 — is Qlar answering, and is reported as such.
    """
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return True
    if response.status_code == 404 and "json" not in content_type:
        return True
    return False
