"""Mail settings and the persisted enrolment state."""

from __future__ import annotations

import json

import pytest

from qlar_email_gateway.config import ConfigError, EnrollmentState, load_settings

BASE_ENV = {
    "QLAR_BASE_URL": "https://api.qlar.test/messenger/api/email-gateway",
    "IMAP_HOST": "imap.corp.test",
    "SMTP_HOST": "smtp.corp.test",
    "MAIL_USER": "Ask@Corp.Test",
    "MAIL_PASSWORD": "s3cret",
}


@pytest.fixture
def env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(
            ("QLAR_", "IMAP_", "SMTP_", "MAIL_", "RECIPIENT_", "MAX_", "POLL_", "GATEWAY_", "AUDIT_")
        ):
            monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_mail_defaults(env):
    settings = load_settings()

    mail = settings.mail
    assert (mail.imap_host, mail.imap_port, mail.imap_security, mail.imap_folder) == (
        "imap.corp.test",
        993,
        "ssl",
        "INBOX",
    )
    assert (mail.smtp_host, mail.smtp_port, mail.smtp_security) == ("smtp.corp.test", 587, "starttls")
    assert mail.password == "s3cret"
    assert settings.poll_interval_seconds == 60
    assert settings.recipient_allowlist == ()
    assert settings.recipient_memory_days == 30
    assert settings.max_sends_per_hour_per_recipient == 20
    assert settings.max_inbound_text_chars == 20000


def test_mail_address_defaults_to_user_lowercased(env):
    assert load_settings().mail.address == "ask@corp.test"

    env.setenv("MAIL_ADDRESS", "Support@Corp.Test")
    assert load_settings().mail.address == "support@corp.test"


def test_allowlist_parses_comma_separated_addresses_and_domains(env):
    env.setenv("RECIPIENT_ALLOWLIST", "a@x.com, @corp.com ,y.org")

    assert load_settings().recipient_allowlist == ("a@x.com", "@corp.com", "y.org")


def test_missing_mailbox_settings_are_named(env):
    env.delenv("MAIL_PASSWORD")
    env.delenv("IMAP_HOST")

    with pytest.raises(ConfigError, match="IMAP_HOST.*MAIL_PASSWORD"):
        load_settings()


def test_security_must_be_ssl_or_starttls(env):
    env.setenv("IMAP_SECURITY", "tls")

    with pytest.raises(ConfigError, match="IMAP_SECURITY"):
        load_settings()


def test_audit_default_is_mail_jsonl(env):
    assert str(load_settings().audit_log_file).replace("\\", "/").endswith("audit/mail.jsonl")


def test_state_round_trip_keeps_mail_fields(tmp_path):
    path = tmp_path / "gateway-state.json"
    state = EnrollmentState(
        gateway_id="egw_1",
        qlar_public_key_pem="PEM",
        enrolled_at="2026-10-07T08:00:00+00:00",
        base_url="https://q/api/email-gateway",
        mailbox_address="ask@corp.test",
        uid_validity=42,
        last_uid=4182,
        recipients={"budi@x.com": "2026-10-07T08:00:00+00:00"},
    )

    state.save(path)
    loaded = EnrollmentState.load(path)

    assert loaded == state
    assert not list(tmp_path.glob("*.tmp")), "save must not leave its temp file behind"


def test_old_state_file_without_mail_fields_loads(tmp_path):
    path = tmp_path / "gateway-state.json"
    path.write_text(
        json.dumps(
            {
                "gatewayId": "egw_1",
                "qlarPublicKeyPem": "PEM",
                "enrolledAt": "2026-10-07T08:00:00+00:00",
                "baseUrl": "https://q/api/email-gateway",
            }
        ),
        encoding="utf-8",
    )

    loaded = EnrollmentState.load(path)

    assert loaded is not None
    assert loaded.mailbox_address == ""
    assert loaded.uid_validity is None
    assert loaded.last_uid is None
    assert loaded.recipients == {}
