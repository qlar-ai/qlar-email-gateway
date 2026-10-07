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


class QlarUnreachable(Exception):
    """Qlar could not be reached — a network problem, not a rejection."""


class QlarRejected(Exception):
    """Qlar answered with a refusal. `status` carries the HTTP code."""

    def __init__(self, status: int, message: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body or {}


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
        timestamp = str(int(time.time()))
        nonce = new_nonce()

        message = canonical_request("POST", path, timestamp, nonce, body)
        headers = {
            "Content-Type": "application/json",
            PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: sign(self.private_key, message),
        }
        if self.gateway_id:
            headers[GATEWAY_ID_HEADER] = self.gateway_id

        url = f"{self.base_url.rstrip('/')}{path}"

        try:
            with httpx.Client(verify=self.verify_tls, timeout=timeout, follow_redirects=False) as client:
                response = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as error:
            raise QlarUnreachable(str(error)) from error

        decoded: dict[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                decoded = parsed if isinstance(parsed, dict) else {"value": parsed}
            except ValueError:
                decoded = {"raw": response.text[:500]}

        if response.status_code >= 400:
            if _is_not_our_api(response, decoded):
                raise QlarNotAnEndpoint(response.status_code, url, response.headers.get("content-type", ""))
            raise QlarRejected(
                response.status_code,
                str(decoded.get("message") or decoded.get("title") or response.reason_phrase),
                decoded,
            )

        return response.status_code, decoded


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
