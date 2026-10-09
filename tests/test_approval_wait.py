"""The wait between enrolling and being approved.

That wait is the normal state of every new gateway, and it used to be indistinguishable from a
failure: `pending_approval` fell through to the generic rejection branch, so the operator saw
`Qlar rejected the poll: HTTP 403: Forbidden` every few seconds with the word "approval"
nowhere on screen. People read that as broken, killed the process, and then had to be told to
start it again after clicking Approve.

These drive the real `PollLoop.run_forever`; only the network call and the sleep are replaced,
because the thing under test is which branch a refusal takes and how long it waits.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from qlar_email_gateway.client import Deleted, QlarRejected
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.poll import (
    APPROVAL_POLL_SECONDS,
    MAX_BACKOFF_SECONDS,
    AwaitingApproval,
    PollLoop,
    Revoked,
)


def _rejection(reason: str, status: int = 403) -> QlarRejected:
    return QlarRejected(status, "Forbidden", {"reason": reason})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://qlar.test/api/email-gateway",
        gateway_name="test gateway",
        enrollment_code=None,
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


def _state() -> EnrollmentState:
    return EnrollmentState(
        gateway_id="gateway-1",
        qlar_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        enrolled_at="2026-09-20T00:00:00Z",
        base_url="https://qlar.test/api/email-gateway",
    )


class _ScriptedLoop:
    """A real PollLoop whose HTTP answers are a script and whose sleeps are recorded.

    The seam is the client, not `_poll_once`: turning a refusal into a named outcome happens
    inside `_poll_once`, so a test that injected there would be testing nothing but itself.
    """

    def __init__(self, tmp_path: Path, answers: list[object]) -> None:
        self.loop = PollLoop(_settings(tmp_path), _state())
        self.answers = list(answers)
        self.sleeps: list[float] = []

        def post(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object]]:
            if not self.answers:
                # Nothing left to say; end the loop the way Ctrl-C would.
                self.loop.stop()
                return 204, {}
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            # 204 is the ordinary "no work for you right now", which is a successful poll.
            return 204, {}

        def sleep_with_jitter(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.loop.client.post = post  # type: ignore[method-assign]
        self.loop._sleep_with_jitter = sleep_with_jitter  # noqa: SLF001 - the timing seam

    def run(self) -> None:
        self.loop.run_forever()


def test_the_wait_is_announced_once_in_words_and_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scripted = _ScriptedLoop(tmp_path, [_rejection("pending_approval")] * 4)

    with caplog.at_level(logging.INFO):
        scripted.run()

    waiting = [r for r in caplog.records if "waiting for approval" in r.message]
    # Said once. Repeating it every few seconds is what made the old message read as a fault.
    assert len(waiting) == 1
    assert waiting[0].levelno == logging.INFO

    # And never as an error, which is what sent operators looking for something to fix.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_waiting_does_not_use_the_error_backoff(tmp_path: Path) -> None:
    scripted = _ScriptedLoop(tmp_path, [_rejection("pending_approval")] * 4)
    scripted.run()

    # A person is looking at a fingerprint; the click should be picked up in seconds, not after
    # the backoff has stretched the interval to fifteen.
    assert scripted.sleeps == [APPROVAL_POLL_SECONDS] * 4
    assert APPROVAL_POLL_SECONDS < MAX_BACKOFF_SECONDS


def test_approval_is_announced_when_the_polling_starts_working(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Two refusals, then Qlar starts answering: exactly what clicking Approve looks like here.
    scripted = _ScriptedLoop(tmp_path, [_rejection("pending_approval"), None])

    with caplog.at_level(logging.INFO):
        scripted.run()

    assert any("approved" in r.message.lower() for r in caplog.records)


def test_revoked_still_stops_the_loop(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # The one refusal that is fatal must stay fatal: a revoked gateway has to stop working.
    scripted = _ScriptedLoop(tmp_path, [Revoked(), None])

    with caplog.at_level(logging.INFO):
        scripted.run()

    assert any("revoked" in r.message.lower() for r in caplog.records)
    # It stopped at the Revoked rather than carrying on to the poll that would have succeeded.
    assert scripted.answers == [None]
    assert isinstance(scripted.loop.stopped_by, Revoked)


def test_deleted_stops_the_loop_and_says_why(tmp_path: Path) -> None:
    scripted = _ScriptedLoop(tmp_path, [_rejection("unknown_gateway"), None])
    scripted.run()

    assert scripted.answers == [None]
    assert isinstance(scripted.loop.stopped_by, Deleted)


def test_other_refusals_still_back_off_and_are_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scripted = _ScriptedLoop(tmp_path, [_rejection("unauthorized")] * 3)

    with caplog.at_level(logging.INFO):
        scripted.run()

    assert scripted.sleeps == [1.0, 2.0, 4.0]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


class TestReasonClassification:
    """`_poll_once` turns a refusal into a named outcome; only two names are acted on."""

    @staticmethod
    def _classify(tmp_path: Path, rejection: QlarRejected) -> type[Exception] | None:
        loop = PollLoop(_settings(tmp_path), _state())

        def post(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object]]:
            raise rejection

        loop.client.post = post  # type: ignore[method-assign]

        try:
            loop._poll_once()  # noqa: SLF001
        except (Revoked, AwaitingApproval) as named:
            return type(named)
        except QlarRejected:
            return None
        return None

    def test_pending_approval_is_named(self, tmp_path: Path) -> None:
        assert self._classify(tmp_path, _rejection("pending_approval")) is AwaitingApproval

    def test_revoked_is_named(self, tmp_path: Path) -> None:
        assert self._classify(tmp_path, _rejection("revoked")) is Revoked

    def test_unknown_gateway_is_named_deleted(self, tmp_path: Path) -> None:
        assert self._classify(tmp_path, _rejection("unknown_gateway")) is Deleted

    @pytest.mark.parametrize("reason", ["unauthorized", "clock_skew", ""])
    def test_everything_else_stays_generic(self, tmp_path: Path, reason: str) -> None:
        assert self._classify(tmp_path, _rejection(reason)) is None

    def test_a_401_is_not_mistaken_for_an_approval_wait(self, tmp_path: Path) -> None:
        # Only 403 carries these reasons; a 401 is a signature or clock problem.
        assert self._classify(tmp_path, _rejection("pending_approval", status=401)) is None
