"""What enrolment tells Qlar about the mailbox, and what it keeps."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from qlar_email_gateway import __version__
from qlar_email_gateway import enroll as enroll_module
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.enroll import EnrollmentError, enroll


def make_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        base_url="https://qlar.test/api/email-gateway",
        gateway_name="support gateway",
        enrollment_code="ABCD-EFGH-JKLM",
        key_file=tmp_path / "gateway-key.pem",
        state_file=tmp_path / "gateway-state.json",
        audit_log_file=None,
        poll_timeout_seconds=25,
        verify_tls=True,
        mail=MailSettings(
            imap_host="imap.corp.test",
            smtp_host="smtp.corp.test",
            user="ask@corp.test",
            password="Unmistakable-Pa55word",
            address="ask@corp.test",
        ),
    )
    return replace(settings, **overrides)


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_post(self, path, payload, *, timeout=30.0):
        seen["path"] = path
        seen["payload"] = payload
        return 200, {"gatewayId": "egw_1", "qlarPublicKeyPem": "QLAR-PEM", "status": "pending_approval"}

    monkeypatch.setattr(enroll_module.QlarClient, "post", fake_post)
    return seen


def test_enroll_sends_mailbox_address_and_idle_flag(tmp_path, captured):
    state, key_fingerprint = enroll(make_settings(tmp_path), idle_probe=lambda mail: True)

    payload = captured["payload"]
    assert captured["path"] == "/enroll"
    assert payload["mailboxAddress"] == "ask@corp.test"
    assert payload["idleSupported"] is True
    assert payload["protocol"] == 1
    assert payload["version"] == __version__
    assert "providers" not in payload
    assert "Unmistakable-Pa55word" not in str(payload.values())
    assert payload["fingerprint"] == key_fingerprint

    assert state.mailbox_address == "ask@corp.test"
    assert state.enrolled_at
    assert EnrollmentState.load(tmp_path / "gateway-state.json").gateway_id == "egw_1"


def test_a_failed_idle_probe_enrols_without_idle(tmp_path, captured):
    def broken(mail):
        raise OSError("unreachable")

    enroll(make_settings(tmp_path), idle_probe=broken)

    assert captured["payload"]["idleSupported"] is False


def test_missing_code_points_at_the_email_panel(tmp_path, captured):
    with pytest.raises(EnrollmentError, match="Channels -> Email"):
        enroll(make_settings(tmp_path, enrollment_code=None), idle_probe=lambda mail: False)
