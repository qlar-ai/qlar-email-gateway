"""What the poll loop does with each job Qlar hands it (PRD FR-32 to FR-35, FR-42, FR-43)."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qlar_email_gateway import crypto
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.poll import PollLoop
from qlar_email_gateway.sender import SmtpSendError
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
    h = Harness(tmp_path, qlar_key)
    h.loop.guard.remember_inbound(["budi@customer.test"])
    return h


def test_send_email_happy_path_posts_ok_result(harness):
    harness.loop._run_job(harness.job())

    assert [job["jobId"] for job in harness.sent] == ["job_1"]
    (result,) = harness.results()
    assert result["status"] == "ok"
    assert result["sentMessageId"] == "<qlar-abc@corp.test>"
    assert isinstance(result["durationMs"], int)
    assert harness.posts[-1][0] == "/jobs/job_1/result"
    entry = harness.audit()[-1]
    assert (entry["status"], entry["agentId"], entry["conversationId"]) == (
        "sent",
        "agt_1",
        "m1@customer.test",
    )


def test_send_is_counted_against_the_rate_limit(tmp_path, qlar_key):
    harness = Harness(tmp_path, qlar_key, max_sends_per_hour_per_recipient=1)
    harness.loop.guard.remember_inbound(["budi@customer.test"])

    harness.loop._run_job(harness.job(jobId="job_1"))
    harness.loop._run_job(harness.job(jobId="job_2"))

    assert len(harness.sent) == 1
    second = harness.results()[-1]
    assert second["error"] == {
        "category": "rejected",
        "code": "rate_limited",
        "messageText": second["error"]["messageText"],
    }


def test_tampered_job_is_dropped_without_result(harness):
    tampered = harness.job()
    tampered["to"] = ["attacker@evil.test"]
    harness.poll_answer = (200, {"job": tampered})

    assert harness.loop._poll_once() is None
    assert harness.results() == []
    assert harness.audit()[-1]["status"] == "bad_signature"


def test_valid_job_is_returned_from_poll(harness):
    job = harness.job()
    harness.poll_answer = (200, {"job": job})

    assert harness.loop._poll_once()["jobId"] == "job_1"


def test_expired_job_reports_expired(harness):
    past = datetime.now(UTC) - timedelta(minutes=1)
    harness.loop._run_job(harness.job(expiresAt=past.strftime("%Y-%m-%dT%H:%M:%SZ")))

    assert harness.sent == []
    assert harness.results()[-1]["error"]["category"] == "expired"
    assert harness.audit()[-1]["status"] == "expired"


def test_unknown_recipient_reports_rejected_and_does_not_send(harness):
    harness.loop._run_job(harness.job(to=["stranger@elsewhere.test"]))

    assert harness.sent == []
    error = harness.results()[-1]["error"]
    assert (error["category"], error["code"]) == ("rejected", "recipient_not_known")
    assert harness.audit()[-1]["status"] == "send_rejected"


def test_smtp_auth_error_reports_auth(harness):
    harness.send_outcome = SmtpSendError("auth", "535", "SMTP login refused")

    harness.loop._run_job(harness.job())

    error = harness.results()[-1]["error"]
    assert (error["category"], error["code"]) == ("auth", "535")
    assert harness.audit()[-1]["status"] == "send_failed"


def test_newer_protocol_job_is_rejected(harness):
    harness.poll_answer = (200, {"job": harness.job(protocol=2)})

    assert harness.loop._poll_once() is None
    assert harness.results()[-1]["error"]["category"] == "rejected"


def test_test_connection_reports_banners(harness):
    harness.loop._run_job(harness.job(type="test_connection", to=[], subject="", textBody="", htmlBody=""))

    result = harness.results()[-1]
    assert result["status"] == "ok"
    assert (result["imapBanner"], result["smtpBanner"], result["idleSupported"], result["inboxCount"]) == (
        "* OK IMAP ready",
        "220 smtp ready",
        True,
        12,
    )
    assert harness.sent == []


def test_test_connection_failure_reports_category(harness):
    harness.mailbox_outcome = SmtpSendError("connection", "", "could not reach the SMTP server")

    harness.loop._run_job(harness.job(type="test_connection", to=[]))

    assert harness.results()[-1]["error"]["category"] == "connection"


def test_poll_body_carries_mailbox_status_and_last_uid(harness):
    harness.status.set("idle_unsupported")

    harness.loop._poll_once()

    path, body = harness.posts[-1]
    assert path == "/jobs/poll"
    assert body["mailboxStatus"] == "idle_unsupported"
    assert body["lastUid"] == 4182
    assert body["protocol"] == 1
    assert body["maxWaitSeconds"] == 25


def test_audit_never_contains_password(harness, caplog):
    harness.send_outcome = SmtpSendError("auth", "535", "SMTP login refused")

    with caplog.at_level(logging.DEBUG):
        harness.loop._run_job(harness.job())
        harness.loop._run_job(harness.job(jobId="job_2"))

    audit_text = harness.settings.audit_log_file.read_text(encoding="utf-8")
    assert PASSWORD not in audit_text
    assert PASSWORD not in caplog.text
    assert all(PASSWORD not in json.dumps(payload) for _, payload in harness.posts)
