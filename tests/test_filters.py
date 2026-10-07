"""Which inbound mail is never sent to Qlar (PRD FR-22)."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from qlar_email_gateway.filters import filter_reason

MAILBOX = "ask@corp.test"
ENROLLED = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


def message(**headers: str) -> EmailMessage:
    msg = EmailMessage()
    defaults = {
        "From": "Budi <budi@customer.test>",
        "To": MAILBOX,
        "Subject": "Harga paket",
        "Date": "Wed, 07 Oct 2026 09:00:00 +0000",
        "Message-ID": "<m1@customer.test>",
    }
    defaults.update({key.replace("_", "-"): value for key, value in headers.items()})
    for key, value in defaults.items():
        if value is not None:
            msg[key] = value
    msg.set_content("Berapa harga paket A?")
    return msg


def reason(**headers: str) -> str | None:
    return filter_reason(message(**headers), MAILBOX, ENROLLED)


def test_a_normal_human_email_is_forwarded():
    assert reason() is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_message_id(value):
    msg = message()
    del msg["Message-ID"]
    if value is not None:
        msg["Message-ID"] = value
    assert filter_reason(msg, MAILBOX, ENROLLED) == "no_message_id"


def test_auto_submitted_no_is_forwarded():
    assert reason(Auto_Submitted="no") is None


@pytest.mark.parametrize("value", ["auto-replied", "auto-generated", "Auto-Notified"])
def test_auto_submitted(value):
    assert reason(Auto_Submitted=value) == "auto_submitted"


@pytest.mark.parametrize("value", ["bulk", "List", "JUNK"])
def test_precedence(value):
    assert reason(Precedence=value) == "precedence"


def test_precedence_other_values_forward():
    assert reason(Precedence="normal") is None


def test_auto_response_suppress():
    assert reason(X_Auto_Response_Suppress="All") == "auto_response_suppress"


@pytest.mark.parametrize("header", ["List_Id", "List_Unsubscribe"])
def test_mailing_list(header):
    assert reason(**{header: "<news.corp.test>"}) == "mailing_list"


def test_own_address_any_case():
    assert reason(From="Support <ASK@Corp.Test>") == "own_address"


@pytest.mark.parametrize(
    "sender",
    [
        "mailer-daemon@corp.test",
        "postmaster@x.test",
        "noreply@x.test",
        "No-Reply@x.com",
        "MAILER-DAEMON@x.test",
    ],
)
def test_system_senders(sender):
    assert reason(From=sender) == "system_sender"


def test_before_enrollment():
    assert reason(Date="Wed, 07 Oct 2026 07:59:00 +0000") == "before_enrollment"


def test_unparseable_date_forwards():
    # Never drop mail over a bad header.
    assert reason(Date="sometime last week") is None


def test_order_auto_submitted_before_own_address():
    assert reason(From=MAILBOX, Auto_Submitted="auto-replied") == "auto_submitted"
