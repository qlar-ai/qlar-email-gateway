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
from qlar_email_gateway.client import Deleted, Revoked
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
    monkeypatch.setattr(cli, "enroll", lambda _s, **_kw: (enrolled, "AA:BB:CC"))

    exit_code = cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True)

    assert exit_code == 0
    # The point of the change: no second command stands between enrolling and working.
    assert [state.gateway_id for state in served] == ["gateway-new"]


def test_an_already_enrolled_machine_skips_enrolment_and_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    _write_state(tmp_path)

    def must_not_enrol(_s: Settings, **_kw: object) -> tuple[EnrollmentState, str]:
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
    monkeypatch.setattr(cli, "enroll", lambda _s, **_kw: (enrolled, "AA:BB:CC"))

    assert cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True) == 0
    assert [state.gateway_id for state in served] == ["gateway-new"]


def test_a_failed_enrolment_does_not_start_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    def refuse(_s: Settings, **_kw: object) -> tuple[EnrollmentState, str]:
        raise EnrollmentError("that code has already been used")

    monkeypatch.setattr(cli, "enroll", refuse)

    assert cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True) == 1
    assert served == []


def _new_state() -> EnrollmentState:
    return EnrollmentState(
        gateway_id="gateway-new",
        qlar_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        enrolled_at="2026-10-09T00:00:00Z",
        base_url=BASE_URL,
    )


def test_a_code_on_the_command_line_enrols_an_enrolled_machine_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    # The old gateway was deleted in the CMS and a new code generated: no file to delete by hand.
    _write_state(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_enroll(_s: Settings, **kwargs: object) -> tuple[EnrollmentState, str]:
        calls.append(kwargs)
        return _new_state(), "AA:BB:CC"

    monkeypatch.setattr(cli, "enroll", fake_enroll)

    exit_code = cli._command_enroll(
        _settings(tmp_path, code=None), tmp_path / ".env", code="NEW1-CODE-HERE", connection_ok=True
    )

    assert exit_code == 0
    assert calls == [{"replace_existing": True}]
    assert [state.gateway_id for state in served] == ["gateway-new"]


def test_a_spent_code_from_shell_history_keeps_the_existing_enrolment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    # Yesterday's command re-run after a reboot: the code is spent, the enrolment is still good.
    _write_state(tmp_path)

    def refuse(_s: Settings, **_kw: object) -> tuple[EnrollmentState, str]:
        raise EnrollmentError("Qlar rejected the enrolment code.")

    monkeypatch.setattr(cli, "enroll", refuse)

    exit_code = cli._command_enroll(
        _settings(tmp_path, code=None), tmp_path / ".env", code="OLD1-CODE-USED", connection_ok=True
    )

    assert exit_code == 0
    assert [state.gateway_id for state in served] == ["gateway-1"]


def test_a_different_endpoint_replaces_the_old_enrolment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[EnrollmentState]
) -> None:
    _write_state(tmp_path, base_url="https://wap-direct.test/api/email-gateway")
    calls: list[dict[str, object]] = []

    def fake_enroll(_s: Settings, **kwargs: object) -> tuple[EnrollmentState, str]:
        calls.append(kwargs)
        return _new_state(), "AA:BB:CC"

    monkeypatch.setattr(cli, "enroll", fake_enroll)

    assert cli._command_enroll(_settings(tmp_path), tmp_path / ".env", connection_ok=True) == 0
    assert calls == [{"replace_existing": True}]


class TestDeletedInTheCms:
    """Qlar answered `unknown_gateway`: the gateway clears its state and, at a terminal, asks again."""

    @staticmethod
    def _serve_once_script(monkeypatch: pytest.MonkeyPatch, outcomes: list[tuple[int, object]]) -> list[str]:
        served: list[str] = []

        def fake_serve_once(_settings: Settings, state: EnrollmentState, _ok: object) -> tuple[int, object]:
            served.append(state.gateway_id)
            return outcomes.pop(0)

        monkeypatch.setattr(cli, "_serve_once", fake_serve_once)
        return served

    def test_without_a_terminal_it_clears_the_state_and_says_what_to_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = _write_state(tmp_path)
        self._serve_once_script(monkeypatch, [(0, Deleted())])
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli._serve(_settings(tmp_path), state, True)

        assert exit_code == 1
        assert not (tmp_path / "gateway-state.json").exists()
        assert EnrollmentState.load(tmp_path / "gateway-state.json.old").gateway_id == "gateway-1"
        assert "enroll --code" in capsys.readouterr().err

    def test_at_a_terminal_it_asks_for_a_new_code_and_carries_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _write_state(tmp_path)
        served = self._serve_once_script(monkeypatch, [(0, Deleted()), (0, None)])
        monkeypatch.setattr(cli, "can_prompt", lambda: True)
        monkeypatch.setattr(cli, "ask_enrollment_code", lambda: "NEW1-CODE-HERE")
        codes: list[str | None] = []

        def fake_enroll(s: Settings, **_kw: object) -> tuple[EnrollmentState, str]:
            codes.append(s.enrollment_code)
            return _new_state(), "AA:BB:CC"

        monkeypatch.setattr(cli, "enroll", fake_enroll)

        assert cli._serve(_settings(tmp_path, code=None), state, True) == 0
        assert codes == ["NEW1-CODE-HERE"]
        assert served == ["gateway-1", "gateway-new"]

    def test_a_revocation_is_left_for_a_human(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Revoked still exists in the CMS; its state is evidence, not clutter.
        state = _write_state(tmp_path)
        self._serve_once_script(monkeypatch, [(0, Revoked())])

        assert cli._serve(_settings(tmp_path), state, True) == 0
        assert (tmp_path / "gateway-state.json").exists()
