import json
import sys

import pytest

from qlar_email_gateway.hosted.credentials import (
    CredentialFormatError,
    fetch_mail_settings_via_control_plane,
    parse_mail_settings,
)

FULL = {
    "imapHost": "imap.example.com",
    "imapPort": 993,
    "imapSecurity": "ssl",
    "smtpHost": "smtp.example.com",
    "smtpPort": 587,
    "smtpSecurity": "starttls",
    "username": "bob@example.com",
    "password": "pässwörd-日本",
    "address": "bob@example.com",
    "fromName": "Bob",
}


def test_parse_happy_path_with_non_ascii_password():
    settings = parse_mail_settings(json.dumps(FULL, ensure_ascii=False))

    assert settings.imap_host == "imap.example.com"
    assert settings.imap_port == 993
    assert settings.imap_security == "ssl"
    assert settings.smtp_host == "smtp.example.com"
    assert settings.smtp_port == 587
    assert settings.smtp_security == "starttls"
    assert settings.user == "bob@example.com"
    assert settings.password == "pässwörd-日本"
    assert settings.address == "bob@example.com"
    assert settings.from_name == "Bob"


def test_missing_field_is_named_without_values():
    payload = {key: value for key, value in FULL.items() if key != "smtpHost"}

    with pytest.raises(CredentialFormatError) as error:
        parse_mail_settings(json.dumps(payload))

    assert "smtpHost" in str(error.value)
    assert FULL["password"] not in str(error.value)


def test_invalid_json_does_not_leak_content():
    with pytest.raises(CredentialFormatError) as error:
        parse_mail_settings("{secret-password")

    assert "secret-password" not in str(error.value)


def test_from_name_optional():
    payload = {key: value for key, value in FULL.items() if key != "fromName"}

    assert parse_mail_settings(json.dumps(payload)).from_name == ""


def test_fetch_reuses_one_credential_and_client_per_vault(monkeypatch):
    import types

    from qlar_email_gateway.hosted import credentials as credentials_module

    made = {"credential": 0, "client": 0}

    class FakeCredential:
        def __init__(self):
            made["credential"] += 1

    class FakeSecret:
        value = json.dumps(FULL)

    class FakeClient:
        def __init__(self, vault_url, credential):
            made["client"] += 1
            self.vault_url = vault_url

        def get_secret(self, name):
            return FakeSecret()

    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = FakeCredential
    secrets = types.ModuleType("azure.keyvault.secrets")
    secrets.SecretClient = FakeClient
    for name, module in {
        "azure": types.ModuleType("azure"),
        "azure.identity": identity,
        "azure.keyvault": types.ModuleType("azure.keyvault"),
        "azure.keyvault.secrets": secrets,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(credentials_module, "_clients", {})

    for secret_name in ("email-hosted-a", "email-hosted-b", "email-hosted-a"):
        assert credentials_module.fetch_mail_settings("https://v1", secret_name).user == FULL["username"]
    credentials_module.fetch_mail_settings("https://v2", "email-hosted-c")

    assert made == {"credential": 2, "client": 2}


def test_base_import_does_not_load_azure():
    assert "azure.identity" not in sys.modules


def test_fetch_via_control_plane_parses_the_returned_json():
    class Client:
        def fetch_credentials(self, gateway_id):
            assert gateway_id == "egw_1"
            return json.dumps(FULL)

    settings = fetch_mail_settings_via_control_plane(Client(), "egw_1")

    assert settings.user == "bob@example.com"
    assert settings.password == FULL["password"]


def test_fetch_via_control_plane_bad_json_never_leaks_the_password():
    class Client:
        def fetch_credentials(self, gateway_id):
            return json.dumps({**FULL, "imapPort": "nope", "smtpHost": ""})

    with pytest.raises(CredentialFormatError) as caught:
        fetch_mail_settings_via_control_plane(Client(), "egw_1")

    assert FULL["password"] not in str(caught.value)
