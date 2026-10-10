"""What the gateway checks before a send (PRD FR-33). Enforced here; nothing in a job can loosen it."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.send_guard import SendGuard


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def make_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        base_url="https://q/api/email-gateway",
        gateway_name="g",
        enrollment_code=None,
        key_file=tmp_path / "key.pem",
        state_file=tmp_path / "state.json",
        audit_log_file=None,
        poll_timeout_seconds=25,
        verify_tls=True,
        mail=MailSettings(
            imap_host="imap", smtp_host="smtp", user="ask@corp.test", password="pw", address="ask@corp.test"
        ),
    )
    return replace(settings, **overrides)


def make_state() -> EnrollmentState:
    return EnrollmentState(gateway_id="egw_1", qlar_public_key_pem="PEM", enrolled_at="x", base_url="b")


@pytest.fixture
def clock() -> Clock:
    return Clock()


def guard_for(tmp_path, clock, **overrides) -> tuple[SendGuard, EnrollmentState]:
    state = make_state()
    return SendGuard(make_settings(tmp_path, **overrides), state, clock=clock), state


def test_anyone_may_be_emailed_case_insensitively(tmp_path, clock):
    # Since 0.1.3 the gateway no longer limits replies to recent senders: Qlar's contact rules decide.
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check(["stranger@elsewhere.test"]) is None
    assert guard.check(["Budi@Customer.Test"]) is None


def test_no_recipient(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check([]) == "no_recipient"
    assert guard.check(["  "]) == "no_recipient"


def test_multiple_recipients_rejected(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check(["a@x.test", "b@x.test"]) == "too_many_recipients"


def test_twenty_first_send_in_an_hour_rate_limited(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)

    for _ in range(20):
        assert guard.check(["budi@customer.test"]) is None
        guard.record_send("budi@customer.test")
        clock.now += timedelta(seconds=30)

    assert guard.check(["budi@customer.test"]) == "rate_limited"

    clock.now += timedelta(hours=1)
    assert guard.check(["budi@customer.test"]) is None


def test_rate_limit_is_per_recipient(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock, max_sends_per_hour_per_recipient=1)
    guard.record_send("a@x.test")

    assert guard.check(["a@x.test"]) == "rate_limited"
    assert guard.check(["b@x.test"]) is None


@pytest.mark.parametrize(
    "smuggled",
    [
        "x@evil.com, y@corp.com",
        "Name <x@evil.com>",
        "x@evil.com y@corp.com",
        "<x@evil.com>",
        "x@evil.com\r\n",
    ],
)
def test_a_recipient_that_is_not_one_bare_address_is_refused(tmp_path, clock, smuggled):
    # "x@evil.com, y@corp.com" is one string in the job, but SMTP would deliver to both.
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check([smuggled]) == "invalid_recipient"
