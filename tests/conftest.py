"""Shared fixtures: a poll-loop harness with a fake Qlar, and the mailbox settings of a reply."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qlar_email_gateway import crypto
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.poll import PollLoop
from qlar_email_gateway.status import MailboxStatus

PASSWORD = "Pa55-w0rd-n3ver-l0gged"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        base_url="https://qlar.test/api/email-gateway",
        gateway_name="test gateway",
        enrollment_code=None,
        key_file=tmp_path / "gateway-key.pem",
        state_file=tmp_path / "gateway-state.json",
        audit_log_file=tmp_path / "audit" / "mail.jsonl",
        poll_timeout_seconds=25,
        verify_tls=True,
        mail=MailSettings(
            imap_host="imap.corp.test",
            smtp_host="smtp.corp.test",
            user="ask@corp.test",
            password=PASSWORD,
            address="ask@corp.test",
        ),
    )
    return replace(settings, **overrides)


@pytest.fixture
def qlar_key():
    return crypto.generate_private_key()


class Harness:
    def __init__(self, tmp_path: Path, qlar_key, **setting_overrides) -> None:
        self.qlar_key = qlar_key
        self.settings = make_settings(tmp_path, **setting_overrides)
        self.state = EnrollmentState(
            gateway_id="egw_1",
            qlar_public_key_pem=crypto.public_key_pem(qlar_key),
            enrolled_at="2026-10-07T08:00:00+00:00",
            base_url=self.settings.base_url,
            mailbox_address="ask@corp.test",
            last_uid=4182,
        )
        self.status = MailboxStatus()
        self.sent: list[dict] = []
        self.send_outcome: object = "<qlar-abc@corp.test>"
        self.mailbox_outcome: object = {
            "imapBanner": "* OK IMAP ready",
            "smtpBanner": "220 smtp ready",
            "idleSupported": True,
            "inboxCount": 12,
        }
        self.loop = PollLoop(
            self.settings,
            self.state,
            status=self.status,
            sender=self._send,
            mailbox_tester=self._test_mailbox,
        )
        self.posts: list[tuple[str, dict]] = []
        self.poll_answer: tuple[int, dict] = (204, {})
        self.loop.client.post = self._post  # type: ignore[method-assign]

    def _send(self, mail, job):
        if isinstance(self.send_outcome, Exception):
            raise self.send_outcome
        self.sent.append(job)
        return self.send_outcome

    def _test_mailbox(self, settings):
        if isinstance(self.mailbox_outcome, Exception):
            raise self.mailbox_outcome
        return self.mailbox_outcome

    def _post(self, path, payload, *, timeout=30.0):
        self.posts.append((path, payload))
        if path == "/jobs/poll":
            return self.poll_answer
        return 200, {"accepted": True}

    def job(self, **fields) -> dict:
        now = datetime.now(UTC)
        job = {
            "jobId": "job_1",
            "type": "send_email",
            "protocol": 1,
            "to": ["budi@customer.test"],
            "subject": "Re: Harga",
            "inReplyTo": "<m1@customer.test>",
            "references": ["<m1@customer.test>"],
            "textBody": "Halo",
            "htmlBody": "<p>Halo</p>",
            "issuedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiresAt": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agentId": "agt_1",
            "userId": "budi@customer.test",
            "conversationId": "m1@customer.test",
        }
        job.update(fields)
        job["signature"] = crypto.sign(self.qlar_key, crypto.canonical_job_bytes(job))
        return job

    def results(self) -> list[dict]:
        return [payload for path, payload in self.posts if path.startswith("/jobs/") and path != "/jobs/poll"]

    def audit(self) -> list[dict]:
        path = self.settings.audit_log_file
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(tmp_path, qlar_key) -> Harness:
    return Harness(tmp_path, qlar_key)


@pytest.fixture
def mail() -> MailSettings:
    return MailSettings(
        imap_host="imap.corp.test",
        smtp_host="smtp.corp.test",
        user="ask@corp.test",
        password="pw",
        address="ask@corp.test",
        from_name="Corp Support",
    )
