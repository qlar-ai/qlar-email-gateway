"""Sending one reply over SMTP from the connected mailbox (PRD FR-34).

What the job may decide: the recipient (already checked by the send guard), the subject, the
threading headers and the two bodies. What it may not: `From` is always the configured mailbox,
attachments are only the pictures the signed job declares, each verified by its SHA-256 before it is
attached (a picture that cannot be verified is left out, never sent unchecked), and every reply
carries `Auto-Submitted: auto-replied` so that other well-behaved systems do not answer it back.
"""

from __future__ import annotations

import re
import smtplib
import ssl
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

from .config import MailSettings
from .job_attachments import Fetched

SMTP_TIMEOUT_SECONDS = 30
_CID_IMG = re.compile(r'<img\b[^>]*\bsrc="cid:[^"]*"[^>]*>', re.IGNORECASE)


class SmtpSendError(Exception):
    """Sending failed. `category` is one of the protocol's result categories."""

    def __init__(self, category: str, code: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.code = code


def build_reply(
    mail: MailSettings,
    job: dict[str, Any],
    message_id: str,
    attachments: list[Fetched] | None = None,
) -> EmailMessage:
    """The reply as a MIME message.

    `attachments` are the verified files of a job that declared some. Pictures with a content id go
    inline, related to the HTML part; the others are ordinary attachments. When the job declared
    attachments but they could not be fetched (`attachments is None`), the reply goes without them
    and without the `<img src="cid:...">` tags that pointed at them.
    """
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
    if attachments is None and job.get("attachments"):
        html = _CID_IMG.sub("", html)
    if html:
        msg.add_alternative(html, subtype="html")

    inline = [item for item in attachments or [] if item.content_id]
    plain = [item for item in attachments or [] if not item.content_id]
    if inline and html:
        html_part = msg.get_body(("html",))
        for item in inline:
            maintype, _, subtype = item.content_type.partition("/")
            html_part.add_related(  # type: ignore[union-attr]
                item.data,
                maintype=maintype,
                subtype=subtype,
                cid=f"<{item.content_id}>",
                disposition="inline",
                filename=item.file_name,
            )
    else:
        plain = [*inline, *plain]
    for item in plain:
        maintype, _, subtype = item.content_type.partition("/")
        msg.add_attachment(
            item.data, maintype=maintype, subtype=subtype, filename=item.file_name, disposition="attachment"
        )
    return msg


def send_reply(
    mail: MailSettings, job: dict[str, Any], attachments: list[Fetched] | None = None
) -> str:
    """Sends the reply and returns the Message-ID it was sent with."""
    domain = mail.address.rpartition("@")[2] or "localhost"
    message_id = f"<qlar-{uuid.uuid4().hex}@{domain}>"
    msg = build_reply(mail, job, message_id, attachments)
    recipients = [str(address) for address in job.get("to") or []]

    with _smtp_errors():
        connection, _ = _connect(mail)
        try:
            connection.login(mail.user, mail.password)
            connection.send_message(msg, from_addr=mail.address, to_addrs=recipients)
        finally:
            _close(connection)

    return message_id


def probe_smtp(mail: MailSettings) -> str:
    """Connects and logs in to SMTP without sending anything; returns the server's banner."""
    with _smtp_errors():
        connection, banner = _connect(mail)
        try:
            connection.login(mail.user, mail.password)
        finally:
            _close(connection)
    return banner


@contextmanager
def _smtp_errors() -> Iterator[None]:
    """Turns smtplib and socket failures into SmtpSendError with a protocol category."""
    try:
        yield
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


def _connect(mail: MailSettings) -> tuple[smtplib.SMTP, str]:
    """Opens the SMTP connection and returns it with the server's greeting banner."""
    context = ssl.create_default_context()
    if mail.smtp_security == "ssl":
        connection: smtplib.SMTP = smtplib.SMTP_SSL(timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        connection = smtplib.SMTP(timeout=SMTP_TIMEOUT_SECONDS)
    # smtplib only remembers the host when it is given to the constructor, and STARTTLS / SMTP_SSL
    # verify the certificate against that remembered name; connect() alone leaves it empty
    # ("check_hostname requires server_hostname"). The constructor would swallow the banner.
    connection._host = mail.smtp_host
    _, banner = connection.connect(mail.smtp_host, mail.smtp_port)
    connection.ehlo()
    if mail.smtp_security == "starttls":
        connection.starttls(context=context)
        connection.ehlo()
    return connection, _decode(banner)


def _close(connection: smtplib.SMTP) -> None:
    try:
        connection.quit()
    except (smtplib.SMTPException, OSError):
        connection.close()


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
