"""Who the gateway may email (PRD FR-33). Enforced here; nothing in a job can loosen it."""

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


def test_reply_to_and_case_insensitive_recipient_allowed(tmp_path, clock):
    guard, state = guard_for(tmp_path, clock)

    guard.remember_inbound(["Budi@Customer.Test", "Team@Customer.Test", None])

    assert guard.check(["budi@customer.test"]) is None
    assert guard.check(["TEAM@customer.test"]) is None
    assert EnrollmentState.load(tmp_path / "state.json").recipients == state.recipients
    assert set(state.recipients) == {"budi@customer.test", "team@customer.test"}


def test_unknown_recipient_rejected(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check(["stranger@elsewhere.test"]) == "recipient_not_known"


def test_no_recipient(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)

    assert guard.check([]) == "no_recipient"
    assert guard.check(["  "]) == "no_recipient"


def test_multiple_recipients_rejected(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)
    guard.remember_inbound(["a@x.test", "b@x.test"])

    assert guard.check(["a@x.test", "b@x.test"]) == "too_many_recipients"


def test_allowlist_address_and_domain_allowed(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock, recipient_allowlist=("a@x.com", "@corp.com", "y.org"))

    assert guard.check(["A@X.com"]) is None
    assert guard.check(["x@corp.com"]) is None
    assert guard.check(["x@y.org"]) is None
    assert guard.check(["x@evilcorp.com"]) == "recipient_not_known"
    assert guard.check(["x@sub.corp.com"]) == "recipient_not_known"


def test_memory_expires_after_days(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)
    guard.remember_inbound(["budi@customer.test"])

    clock.now += timedelta(days=29, hours=23)
    assert guard.check(["budi@customer.test"]) is None

    clock.now += timedelta(days=1, hours=2)
    assert guard.check(["budi@customer.test"]) == "recipient_not_known"


def test_twenty_first_send_in_an_hour_rate_limited(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock)
    guard.remember_inbound(["budi@customer.test"])

    for _ in range(20):
        assert guard.check(["budi@customer.test"]) is None
        guard.record_send("budi@customer.test")
        clock.now += timedelta(seconds=30)

    assert guard.check(["budi@customer.test"]) == "rate_limited"

    clock.now += timedelta(hours=1)
    assert guard.check(["budi@customer.test"]) is None


def test_rate_limit_is_per_recipient(tmp_path, clock):
    guard, _ = guard_for(tmp_path, clock, max_sends_per_hour_per_recipient=1)
    guard.remember_inbound(["a@x.test", "b@x.test"])
    guard.record_send("a@x.test")

    assert guard.check(["a@x.test"]) == "rate_limited"
    assert guard.check(["b@x.test"]) is None
