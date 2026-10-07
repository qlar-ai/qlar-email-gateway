"""Sending one reply over SMTP from the connected mailbox (PRD FR-34).

What the job may decide: the recipient (already checked by the send guard), the subject, the
threading headers and the two bodies. What it may not: `From` is always the configured mailbox,
there are never attachments, and every reply carries `Auto-Submitted: auto-replied` so that other
well-behaved systems do not answer it back.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

from .config import MailSettings

SMTP_TIMEOUT_SECONDS = 30


class SmtpSendError(Exception):
    """Sending failed. `category` is one of the protocol's result categories."""

    def __init__(self, category: str, code: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.code = code


def build_reply(mail: MailSettings, job: dict[str, Any], message_id: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((mail.from_name, mail.address)) if mail.from_name else mail.address
    msg["To"] = ", ".join(str(address) for address in job.get("to") or [])
    msg["Subject"] = str(job.get("subject") or "")
    if job.get("inReplyTo"):
        msg["In-Reply-To"] = str(job["inReplyTo"])
    references = [str(reference) for reference in job.get("references") or []]
    if references:
        msg["References"] = " ".join(references)
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Auto-Submitted"] = "auto-replied"
    if job.get("agentId"):
        msg["X-Qlar-Agent"] = str(job["agentId"])

    msg.set_content(str(job.get("textBody") or ""))
    html = str(job.get("htmlBody") or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_reply(mail: MailSettings, job: dict[str, Any]) -> str:
    """Sends the reply and returns the Message-ID it was sent with."""
    domain = mail.address.rpartition("@")[2] or "localhost"
    message_id = f"<qlar-{uuid.uuid4().hex}@{domain}>"
    msg = build_reply(mail, job, message_id)
    recipients = [str(address) for address in job.get("to") or []]

    try:
        connection = _connect(mail)
        try:
            connection.login(mail.user, mail.password)
            connection.send_message(msg, from_addr=mail.address, to_addrs=recipients)
        finally:
            try:
                connection.quit()
            except (smtplib.SMTPException, OSError):
                connection.close()
    except smtplib.SMTPAuthenticationError as error:
        raise SmtpSendError("auth", str(error.smtp_code), "SMTP login refused") from error
    except smtplib.SMTPRecipientsRefused as error:
        code, reason = next(iter(error.recipients.values()), (0, b""))
        raise SmtpSendError("smtp_rejected", str(code), _decode(reason) or "recipient refused") from error
    except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
        raise SmtpSendError("smtp_rejected", str(error.smtp_code), _decode(error.smtp_error)) from error
    except TimeoutError as error:
        raise SmtpSendError("timeout", "", "the SMTP server did not answer in time") from error
    except (smtplib.SMTPException, OSError) as error:
        raise SmtpSendError(
            "connection", "", f"could not reach the SMTP server: {type(error).__name__}"
        ) from error

    return message_id


def _connect(mail: MailSettings) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if mail.smtp_security == "ssl":
        return smtplib.SMTP_SSL(mail.smtp_host, mail.smtp_port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    connection = smtplib.SMTP(mail.smtp_host, mail.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
    connection.ehlo()
    connection.starttls(context=context)
    connection.ehlo()
    return connection


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
