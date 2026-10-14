import json
import sys

import pytest

from qlar_email_gateway.hosted.credentials import CredentialFormatError, parse_mail_settings

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


def test_base_import_does_not_load_azure():
    assert "azure.identity" not in sys.modules
