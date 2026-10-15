"""Client for Messenger-BE's hosted-worker control plane (`/hosted/*`).

Authenticated with a shared key in `X-Internal-Api-Key`, not the signed protocol the on-prem
gateway speaks: this process runs inside Qlar, next to the service it talks to.
"""

from __future__ import annotations

from typing import Any

import httpx

#: How long the server holds a quiet poll. Sent explicitly: the server's maximum, under APIM's 30 s.
POLL_WAIT_SECONDS = 25
#: Longer than the server's 25 s long-poll so the server, not this client, ends a quiet wait.
POLL_TIMEOUT_SECONDS = 40.0
REQUEST_TIMEOUT_SECONDS = 30.0
API_KEY_HEADER = "X-Internal-Api-Key"  # noqa: S105 - a header name, not a secret


class ControlPlaneAuthError(Exception):
    """The control plane refused the worker's key (401/403): a deployment error, not a retry."""


class ControlPlaneUnreachable(Exception):
    """The control plane could not be reached, or answered with a server error."""


class CredentialsNotAvailable(Exception):
    """The control plane has no credentials to give this mailbox (404: unknown, revoked, none stored)."""


class ControlPlaneRejected(Exception):
    """The control plane refused a request with a 4xx other than 401/403 (not found, bad request)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class ControlPlaneClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={API_KEY_HEADER: api_key}, verify=verify_tls, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def list_registrations(self) -> list[dict[str, Any]]:
        response = self._send("GET", "/hosted/registrations")
        return list(response.json().get("registrations") or [])

    def poll(self) -> dict[str, Any] | None:
        response = self._send(
            "POST",
            "/hosted/poll",
            json={"maxWaitSeconds": POLL_WAIT_SECONDS},
            timeout=POLL_TIMEOUT_SECONDS,
        )
        if response.status_code == 204:
            return None
        return response.json().get("command")

    def post_result(self, control_id: str, payload: dict[str, Any]) -> None:
        self._send("POST", f"/hosted/control/{control_id}/result", json=payload)

    def rekey(self, gateway_id: str) -> str:
        response = self._send("POST", f"/hosted/registrations/{gateway_id}/rekey", json={})
        return str(response.json()["enrollmentCode"])

    def fetch_credentials(self, gateway_id: str) -> str:
        """The mailbox credential JSON as raw text. Never logged; the caller parses it."""
        path = f"/hosted/registrations/{gateway_id}/credentials"
        try:
            return self._send("GET", path).text
        except ControlPlaneRejected as error:
            if error.status == 404:
                raise CredentialsNotAvailable(f"no credentials available for {gateway_id}") from None
            raise

    def _send(
        self, method: str, path: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._http.request(method, self._base_url + path, timeout=timeout, **kwargs)
        except httpx.TransportError as error:
            raise ControlPlaneUnreachable(f"{type(error).__name__} calling {path}") from error

        if response.status_code in (401, 403):
            raise ControlPlaneAuthError(
                f"the control plane refused the worker key (HTTP {response.status_code})"
            )
        if response.status_code >= 500:
            raise ControlPlaneUnreachable(f"HTTP {response.status_code} from {path}")
        if response.status_code >= 400:
            raise ControlPlaneRejected(response.status_code, f"{method} {path}")
        return response
