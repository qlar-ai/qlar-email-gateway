"""What the poll loop does with each job Qlar hands it (PRD FR-32 to FR-35, FR-42, FR-43)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from conftest import PASSWORD, Harness

from qlar_email_gateway.sender import SmtpSendError


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


def test_a_recipient_who_never_wrote_in_is_sent_to(harness):
    harness.loop._run_job(harness.job(to=["stranger@elsewhere.test"]))

    assert [job["jobId"] for job in harness.sent] == ["job_1"]
    assert harness.results()[-1]["status"] == "ok"


def test_two_recipients_report_rejected_and_do_not_send(harness):
    harness.loop._run_job(harness.job(to=["a@x.test", "b@x.test"]))

    assert harness.sent == []
    error = harness.results()[-1]["error"]
    assert (error["category"], error["code"]) == ("rejected", "too_many_recipients")
    assert harness.audit()[-1]["status"] == "send_rejected"


def test_smtp_auth_error_reports_auth(harness):
    harness.send_outcome = SmtpSendError("auth", "535", "SMTP login refused")

    harness.loop._run_job(harness.job())

    error = harness.results()[-1]["error"]
    assert (error["category"], error["code"]) == ("auth", "535")
    assert harness.audit()[-1]["status"] == "send_failed"


def test_newer_protocol_job_is_rejected(harness):
    harness.poll_answer = (200, {"job": harness.job(protocol=3)})

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
    assert body["protocol"] == 2
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
