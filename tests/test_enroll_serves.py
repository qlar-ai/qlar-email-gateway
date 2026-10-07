"""`enroll` is the whole installation: it enrols if needed, then serves.

There used to be a handover in the middle. `enroll` printed a fingerprint and exited, and the
operator was told to come back and type `run` once someone had clicked Approve — a step across
a wait of unknown length, often in another window on another day. It was missed often enough
that "the CMS says approved but nothing works" became the common failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qlar_email_gateway import cli
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.enroll import EnrollmentError

BASE_URL = "https://qlar.test/api/email-gateway"


def _settings(tmp_path: Path, code: str | None = "ABCD-EFGH-JKLM") -> Settings:
    return Settings(
        base_url=BASE_URL,
        gateway_name="test gateway",
        enrollment_code=code,
        key_file=tmp_path / "gateway-key.pem",
        state_file=tmp_path / "gateway-state.json",
        audit_log_file=None,
        poll_timeout_seconds=25,
        verify_tls=True,
        mail=MailSettings(
            imap_host="imap.test",
            smtp_host="smtp.test",
            user="ask@corp.test",
            password="secret",
            address="ask@corp.test",
        ),
    )


def _write_state(tmp_path: Path, base_url: str = BASE_URL) -> EnrollmentState:
    state = EnrollmentState(
        gateway_id="gateway-1",
        qlar_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        enrolled_at="2026-09-20T00:00:00Z",
        base_url=base_url,
    )
    state.save(tmp_path / "gateway-state.json")
    return state


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[EnrollmentState]:
    """Records the enrolments `_serve` was asked to work with, instead of polling."""
    calls: list[EnrollmentState] = []

    def fake_serve(_settings: Settings, state: EnrollmentState, _connection_ok: object) -> int:
        calls.append(state)
        return 0

    monkeypatch.setattr(cli, "_serve", fake_serve)
    return calls


def test_enrolling_flows_straight_into_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    enrolled = EnrollmentState(
        gateway_id="gateway-new",
        qlar_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        enrolled_at="2026-09-20T00:00:00Z",
        base_url=BASE_URL,
    )
    monkeypatch.setattr(cli, "enroll", lambda _s: (enrolled, "AA:BB:CC"))

    exit_code = cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True)

    assert exit_code == 0
    # The point of the change: no second command stands between enrolling and working.
    assert [state.gateway_id for state in served] == ["gateway-new"]


def test_an_already_enrolled_machine_skips_enrolment_and_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    _write_state(tmp_path)

    def must_not_enrol(_s: Settings) -> tuple[EnrollmentState, str]:
        raise AssertionError("a redeemed code cannot be redeemed again")

    monkeypatch.setattr(cli, "enroll", must_not_enrol)

    # This is a container restart, and an operator restarting a gateway that died: the code was
    # single use and is long gone, so re-enrolling is not merely wasteful but impossible.
    exit_code = cli._command_enroll(_settings(tmp_path, code=None), tmp_path / ".env", connection_ok=True)

    assert exit_code == 0
    assert [state.gateway_id for state in served] == ["gateway-1"]


def test_state_for_a_different_endpoint_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    # Pointed at a different Qlar: that enrolment says nothing about this one, so it enrols.
    _write_state(tmp_path, base_url="https://other.test/api/email-gateway")

    enrolled = EnrollmentState(
        gateway_id="gateway-new",
        qlar_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        enrolled_at="2026-09-20T00:00:00Z",
        base_url=BASE_URL,
    )
    monkeypatch.setattr(cli, "enroll", lambda _s: (enrolled, "AA:BB:CC"))

    assert cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True) == 0
    assert [state.gateway_id for state in served] == ["gateway-new"]


def test_a_failed_enrolment_does_not_start_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    def refuse(_s: Settings) -> tuple[EnrollmentState, str]:
        raise EnrollmentError("that code has already been used")

    monkeypatch.setattr(cli, "enroll", refuse)

    assert cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True) == 1
    assert served == []
