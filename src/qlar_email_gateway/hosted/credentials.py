"""Mailbox credentials for hosted mailboxes: read from Key Vault, parsed into `MailSettings`."""

from __future__ import annotations

import json
import threading
from typing import Any

from ..config import MailSettings

REQUIRED_FIELDS = (
    "imapHost",
    "imapPort",
    "imapSecurity",
    "smtpHost",
    "smtpPort",
    "smtpSecurity",
    "username",
    "password",
    "address",
)


class CredentialFormatError(Exception):
    """The credential JSON is unusable. Names fields only, never values."""


def parse_mail_settings(secret_json: str) -> MailSettings:
    try:
        data: Any = json.loads(secret_json)
    except ValueError:
        raise CredentialFormatError("the credential secret is not valid JSON") from None
    if not isinstance(data, dict):
        raise CredentialFormatError("the credential secret is not a JSON object")

    missing = [name for name in REQUIRED_FIELDS if data.get(name) in (None, "")]
    if missing:
        raise CredentialFormatError("the credential secret is missing: " + ", ".join(missing))

    try:
        return MailSettings(
            imap_host=str(data["imapHost"]),
            imap_port=int(data["imapPort"]),
            imap_security=_security(data["imapSecurity"], "imapSecurity"),
            smtp_host=str(data["smtpHost"]),
            smtp_port=int(data["smtpPort"]),
            smtp_security=_security(data["smtpSecurity"], "smtpSecurity"),
            user=str(data["username"]),
            password=str(data["password"]),
            address=str(data["address"]),
            from_name=str(data.get("fromName") or ""),
        )
    except (TypeError, ValueError) as error:
        raise CredentialFormatError(f"the credential secret has an invalid field: {error}") from None


def _security(value: Any, name: str) -> Any:
    lowered = str(value).lower()
    if lowered not in ("ssl", "starttls"):
        raise ValueError(f"{name} must be ssl or starttls")
    return lowered


#: One SecretClient (and so one DefaultAzureCredential and its token cache) per vault, shared by
#: every mailbox: a fresh credential per read would fetch a new managed-identity token every time.
_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


def _secret_client(vault_uri: str) -> Any:
    with _clients_lock:
        client = _clients.get(vault_uri)
        if client is None:
            # Imported here so a base (on-prem) install, which lacks the `hosted` extra, never needs them.
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            client = SecretClient(vault_url=vault_uri, credential=DefaultAzureCredential())
            _clients[vault_uri] = client
        return client


def fetch_mail_settings(vault_uri: str, secret_name: str) -> MailSettings:
    secret = _secret_client(vault_uri).get_secret(secret_name)
    return parse_mail_settings(secret.value or "")


def fetch_mail_settings_via_control_plane(client: Any, gateway_id: str) -> MailSettings:
    """Reads the credential JSON from Messenger-BE (it stores it encrypted) and parses it."""
    return parse_mail_settings(client.fetch_credentials(gateway_id))
