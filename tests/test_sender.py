"""Sending a reply over SMTP (PRD FR-34) and naming what went wrong."""

from __future__ import annotations

import smtplib
from email import message_from_bytes, policy

import pytest

from qlar_email_gateway import sender
from qlar_email_gateway.config import MailSettings
from qlar_email_gateway.sender import SmtpSendError, send_reply

MAIL = MailSettings(
    imap_host="imap.corp.test",
    smtp_host="smtp.corp.test",
    user="ask@corp.test",
    password="pw",
    address="ask@corp.test",
    from_name="Corp Support",
)

JOB = {
    "jobId": "job_1",
    "type": "send_email",
    "to": ["budi@customer.test"],
    "subject": "Re: Harga paket",
    "inReplyTo": "<m2@customer.test>",
    "references": ["<root@customer.test>", "<m2@customer.test>"],
    "textBody": "Halo Budi, harganya Rp100rb.",
    "htmlBody": "<p>Halo Budi, harganya <strong>Rp100rb</strong>.</p>",
    "agentId": "agt_1",
}


class Recorder:
    """Stands in for smtplib.SMTP / SMTP_SSL and remembers what was asked of it."""

    instances: list[Recorder] = []
    fail_with: Exception | None = None
    fail_at: str = "login"

    def __init__(self, host="", port=0, timeout=None, **kwargs):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[tuple[str, list[str], bytes]] = []
        Recorder.instances.append(self)

    def connect(self, host, port):
        self.host, self.port = host, port
        self._maybe_fail("connect")
        return 220, b"smtp.corp.test ESMTP ready"

    def _maybe_fail(self, step: str) -> None:
        if Recorder.fail_with is not None and Recorder.fail_at == step:
            raise Recorder.fail_with

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")
        self._maybe_fail("login")

    def sendmail(self, from_addr, to_addrs, msg):
        self._maybe_fail("send")
        self.sent.append((from_addr, list(to_addrs), msg if isinstance(msg, bytes) else msg.encode()))
        return {}

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self._maybe_fail("send")
        self.sent.append((from_addr, list(to_addrs), msg.as_bytes(policy=policy.SMTP)))
        return {}

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.quit()


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    Recorder.instances = []
    Recorder.fail_with = None
    Recorder.fail_at = "login"
    monkeypatch.setattr(sender.smtplib, "SMTP", Recorder)
    monkeypatch.setattr(sender.smtplib, "SMTP_SSL", Recorder)
    return Recorder


def sent_message():
    ((_, _, raw),) = Recorder.instances[-1].sent
    return message_from_bytes(raw, policy=policy.default)


def test_headers_and_multipart():
    message_id = send_reply(MAIL, JOB)

    connection = Recorder.instances[-1]
    assert (connection.host, connection.port, connection.timeout) == ("smtp.corp.test", 587, 30)
    assert "starttls" in connection.calls
    assert "login:ask@corp.test" in connection.calls

    msg = sent_message()
    assert msg["From"] == '"Corp Support" <ask@corp.test>' or msg["From"] == "Corp Support <ask@corp.test>"
    assert msg["To"] == "budi@customer.test"
    assert msg["Subject"] == "Re: Harga paket"
    assert msg["In-Reply-To"] == "<m2@customer.test>"
    assert msg["References"] == "<root@customer.test> <m2@customer.test>"
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["X-Qlar-Agent"] == "agt_1"
    assert msg["Date"]
    assert msg["Message-ID"] == message_id
    assert message_id.startswith("<qlar-") and message_id.endswith("@corp.test>")
    assert msg.get_content_type() == "multipart/alternative"
    parts = {part.get_content_type(): part.get_content() for part in msg.iter_parts()}
    assert parts["text/plain"].strip() == "Halo Budi, harganya Rp100rb."
    assert "<strong>Rp100rb</strong>" in parts["text/html"]
    assert Recorder.instances[-1].sent[0][1] == ["budi@customer.test"]


def test_ssl_uses_smtp_ssl_without_starttls():
    mail = MailSettings(
        imap_host="i",
        smtp_host="smtp.corp.test",
        user="u",
        password="p",
        address="ask@corp.test",
        smtp_port=465,
        smtp_security="ssl",
    )

    send_reply(mail, JOB)

    assert "starttls" not in Recorder.instances[-1].calls


def test_from_is_always_the_mailbox_even_if_job_says_otherwise():
    send_reply(MAIL, {**JOB, "from": "ceo@corp.test", "From": "ceo@corp.test"})

    msg = sent_message()
    assert "ask@corp.test" in msg["From"]
    assert Recorder.instances[-1].sent[0][0] == "ask@corp.test"


def test_bare_from_when_no_name():
    mail = MailSettings(imap_host="i", smtp_host="s", user="u", password="p", address="ask@corp.test")

    send_reply(mail, JOB)

    assert sent_message()["From"] == "ask@corp.test"


def test_auth_error_maps_to_auth():
    Recorder.fail_with = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    with pytest.raises(SmtpSendError) as error:
        send_reply(MAIL, JOB)

    assert (error.value.category, error.value.code) == ("auth", "535")


def test_recipients_refused_maps_to_smtp_rejected():
    Recorder.fail_at = "send"
    Recorder.fail_with = smtplib.SMTPRecipientsRefused({"budi@customer.test": (550, b"5.1.1 no such user")})

    with pytest.raises(SmtpSendError) as error:
        send_reply(MAIL, JOB)

    assert (error.value.category, error.value.code) == ("smtp_rejected", "550")


def test_socket_timeout_maps_to_timeout():
    Recorder.fail_at = "connect"
    Recorder.fail_with = TimeoutError("timed out")

    with pytest.raises(SmtpSendError) as error:
        send_reply(MAIL, JOB)

    assert error.value.category == "timeout"


def test_connection_refused_maps_to_connection():
    Recorder.fail_at = "connect"
    Recorder.fail_with = ConnectionRefusedError(111, "refused")

    with pytest.raises(SmtpSendError) as error:
        send_reply(MAIL, JOB)

    assert error.value.category == "connection"


def test_password_never_in_error_text():
    Recorder.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")

    with pytest.raises(SmtpSendError) as error:
        send_reply(MAIL, JOB)

    assert "pw" not in str(error.value).split()
