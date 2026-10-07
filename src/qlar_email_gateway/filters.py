"""Which inbound mail is never forwarded to Qlar (PRD FR-22).

The agent answers every email a *person* sends to the mailbox. Machines are the problem: an
out-of-office reply to the agent's reply, answered by the agent, answered by the out-of-office
again, is a loop that only ends when someone notices the bill. So anything that announces itself
as automated, bulk or list mail, anything from the mailbox itself, and the usual system senders
are dropped here, before a byte of it leaves the network.

Mail dated before enrolment is dropped too: connecting a mailbox must never answer its history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

#: Local parts that are never a person. Matched case-insensitively.
SYSTEM_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster", "noreply", "no-reply"})

#: Precedence values that mean "not a conversation".
BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})


def filter_reason(msg: EmailMessage, mailbox_address: str, enrolled_at: datetime) -> str | None:
    """Returns why this email must not be forwarded, or None to forward it.

    Checked in a fixed order so the audit log names the most specific reason first.
    """
    if not (msg.get("Message-ID") or "").strip():
        return "no_message_id"

    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return "auto_submitted"

    if (msg.get("Precedence") or "").strip().lower() in BULK_PRECEDENCE:
        return "precedence"

    if (msg.get("X-Auto-Response-Suppress") or "").strip():
        return "auto_response_suppress"

    if msg.get("List-Id") is not None or msg.get("List-Unsubscribe") is not None:
        return "mailing_list"

    sender = parseaddr(str(msg.get("From") or ""))[1].strip().lower()
    if sender and sender == mailbox_address.strip().lower():
        return "own_address"

    if sender.partition("@")[0] in SYSTEM_LOCAL_PARTS:
        return "system_sender"

    sent_at = _parse_date(msg.get("Date"))
    if sent_at is not None and sent_at < enrolled_at:
        return "before_enrollment"

    return None


def _parse_date(value: object) -> datetime | None:
    """The Date header as an aware datetime, or None when it cannot be read.

    An unreadable Date forwards the mail: dropping a person's email over a malformed header would
    be a silent loss, and the enrolment cut-off is the only thing the date is used for.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
