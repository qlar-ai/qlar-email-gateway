"""The job loop: ask Qlar for work, do it, send the answer back.

Outbound only. The gateway opens a long-poll request that Qlar holds for up to ~25 seconds and
answers either with a job or with `204 No Content`; either way the gateway immediately asks again.
Nothing listens on a port, nothing needs a certificate, and nothing has to be reachable from the
internet. Every poll also reports the mailbox health and the last forwarded UID, so polling
doubles as the heartbeat.

Two kinds of job arrive here: `send_email` (one reply, through the send guard, over SMTP) and
`test_connection` (log in to IMAP and SMTP and report back). Every job is verified against the Qlar
public key pinned at enrolment before anything else happens; one that fails is dropped without an
answer, because answering would tell whoever forged it that the gateway is listening.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from . import PROTOCOL_VERSION, __version__
from . import mailbox as mailbox_module
from .audit import AuditLog
from .client import (
    Deleted,
    QlarClient,
    QlarNotAnEndpoint,
    QlarRejected,
    QlarUnreachable,
    Revoked,
    stop_reason,
)
from .config import MAX_CONCURRENT_JOBS, EnrollmentState, MailSettings, Settings
from .crypto import load_or_create_private_key, verify_job
from .send_guard import SendGuard
from .sender import SmtpSendError, send_reply
from .status import MailboxStatus

logger = logging.getLogger("qlar_email_gateway.poll")

POLL_PATH = "/jobs/poll"
RESULT_PATH = "/jobs/{job_id}/result"

MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

# Waiting to be approved is not a failure, so it does not use the error backoff. A human is
# looking at the screen in Qlar with the fingerprint in front of them; the gateway should start
# working within a few seconds of the click, not up to fifteen.
APPROVAL_POLL_SECONDS = 4.0

# How many times a finished result is re-sent if the network drops on the way back. Posting a
# result is idempotent on Qlar's side (keyed by job id), so a retry is safe.
RESULT_ATTEMPTS = 4

__all__ = ["APPROVAL_POLL_SECONDS", "MAX_BACKOFF_SECONDS", "AwaitingApproval", "PollLoop", "Revoked"]


class AwaitingApproval(Exception):
    """Enrolled, but no human has confirmed the fingerprint yet. The loop waits, quietly.

    This is the normal state of a gateway for the minute or two between installing it and someone
    clicking Approve, and it is reported as a wait rather than as an error.
    """


class PollLoop:
    def __init__(
        self,
        settings: Settings,
        state: EnrollmentState,
        *,
        status: MailboxStatus | None = None,
        guard: SendGuard | None = None,
        audit: AuditLog | None = None,
        sender: Callable[[MailSettings, dict[str, Any]], str] = send_reply,
        mailbox_tester: Callable[[Settings], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        private_key, _ = load_or_create_private_key(settings.key_file)
        self.client = QlarClient(
            base_url=settings.base_url,
            private_key=private_key,
            gateway_id=state.gateway_id,
            verify_tls=settings.verify_tls,
        )
        self.audit = audit or AuditLog(settings.audit_log_file)
        self.status = status or MailboxStatus()
        self.guard = guard or SendGuard(settings, state)
        self._send = sender
        self._test_mailbox = mailbox_tester or mailbox_module.test_mailbox
        self._stopping = threading.Event()
        #: Set when Qlar told the loop to stop for good, so the caller can tell a deletion (clear
        #: the state, enrol again) from a revocation (leave everything for a human to look at).
        self.stopped_by: Revoked | None = None
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="qlar-job")

    def stop(self) -> None:
        self._stopping.set()

    def run_forever(self) -> None:
        """Polls until stopped or revoked. A revocation stops the loop rather than raising."""
        backoff = MIN_BACKOFF_SECONDS
        announced_waiting = False
        logger.info(
            "gateway %s starting, polling %s (protocol %d, version %s)",
            self.state.gateway_id,
            self.settings.base_url,
            PROTOCOL_VERSION,
            __version__,
        )

        while not self._stopping.is_set():
            if self._current_in_flight() >= MAX_CONCURRENT_JOBS:
                time.sleep(0.25)
                continue

            try:
                job = self._poll_once()
                backoff = MIN_BACKOFF_SECONDS
                if announced_waiting:
                    logger.info("approved. Answering email for %s", self.settings.mail.address)
                    announced_waiting = False
                if job is not None:
                    self._dispatch(job)
            except AwaitingApproval:
                if not announced_waiting:
                    logger.info(
                        "enrolled, waiting for approval in Qlar. Compare the key fingerprint above "
                        "with the one the CMS shows and click Approve; this starts working on its own, "
                        "nothing else to run here.",
                    )
                    announced_waiting = True
                self._sleep_with_jitter(APPROVAL_POLL_SECONDS)
            except Deleted as deleted:
                logger.error("this gateway has been deleted in the Qlar CMS; stopping")
                self.stopped_by = deleted
                break
            except Revoked as revoked:
                logger.error(
                    "this gateway has been revoked in the Qlar CMS; stopping. To reconnect, generate "
                    "a new code in the CMS and run: qlar-email-gateway enroll --code <code>"
                )
                self.stopped_by = revoked
                break
            except QlarUnreachable as error:
                logger.warning("Qlar unreachable (%s); retrying in %.1fs", error, backoff)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarNotAnEndpoint as wrong_address:
                logger.error(
                    "%s. That is not Qlar's API: check QLAR_BASE_URL (%s), which must end in "
                    "/api/email-gateway. Retrying in %.1fs",
                    wrong_address,
                    self.settings.base_url,
                    backoff,
                )
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarRejected as rejection:
                logger.error("Qlar rejected the poll: %s", rejection)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        self._pool.shutdown(wait=True, cancel_futures=False)
        logger.info("job loop stopped")

    def _poll_once(self) -> dict[str, Any] | None:
        payload = {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "mailboxStatus": self.status.get(),
            "lastUid": self.state.last_uid,
            "maxWaitSeconds": self.settings.poll_timeout_seconds,
        }

        try:
            status, body = self.client.post(
                POLL_PATH,
                payload,
                # Comfortably longer than the server's hold, so a normal empty poll is not
                # mistaken for a network failure.
                timeout=self.settings.poll_timeout_seconds + 15,
            )
        except QlarRejected as rejection:
            if (stop := stop_reason(rejection)) is not None:
                raise stop from rejection
            reason = str(rejection.body.get("reason", "")).lower()
            if rejection.status == 403 and reason == "pending_approval":
                raise AwaitingApproval from rejection
            raise

        if status == 204 or not body:
            return None

        job = body.get("job") if "job" in body else body
        if not isinstance(job, dict) or not job.get("jobId"):
            return None

        # The job is signed by Qlar itself. Verifying it here means a proxy at the customer's own
        # edge cannot redirect or rewrite a reply, even though it terminates the TLS.
        if not verify_job(self.state.qlar_public_key_pem, job):
            logger.error("job %s failed signature verification and was discarded", job.get("jobId"))
            self.audit.record(status="bad_signature", job_id=str(job.get("jobId")))
            return None

        if int(job.get("protocol", PROTOCOL_VERSION)) > PROTOCOL_VERSION:
            logger.error(
                "job %s needs protocol %s but this gateway speaks %d - upgrade the gateway",
                job.get("jobId"),
                job.get("protocol"),
                PROTOCOL_VERSION,
            )
            self._send_result(
                str(job["jobId"]),
                _error(
                    "rejected",
                    "protocol_too_new",
                    f"gateway speaks protocol {PROTOCOL_VERSION}; job requires {job.get('protocol')}. "
                    "Upgrade the on-premise gateway.",
                ),
            )
            return None

        return job

    def _dispatch(self, job: dict[str, Any]) -> None:
        with self._in_flight_lock:
            self._in_flight += 1
        self._pool.submit(self._run_and_release, job)

    def _run_and_release(self, job: dict[str, Any]) -> None:
        try:
            self._run_job(job)
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("jobId"))
        job_type = str(job.get("type", ""))
        started = time.monotonic()

        try:
            if _is_expired(job.get("expiresAt")):
                logger.info("job %s expired before it started; skipping", job_id)
                self._audit_job(job, "expired", reason="job expired before it was run")
                self._send_result(job_id, _error("expired", "expired", "job expired before it was run"))
                return

            if job_type == "send_email":
                self._run_send_email(job, started)
            elif job_type == "test_connection":
                self._run_test_connection(job, started)
            else:
                self._send_result(
                    job_id, _error("rejected", "unknown_job_type", f"unknown job type {job_type!r}")
                )
        except Exception as error:  # noqa: BLE001 - a worker must never die silently
            logger.exception("job %s failed unexpectedly", job_id)
            self._send_result(
                job_id, _error("internal", type(error).__name__, "the gateway failed unexpectedly")
            )

    def _run_send_email(self, job: dict[str, Any], started: float) -> None:
        job_id = str(job["jobId"])
        recipients = [str(address) for address in job.get("to") or []]

        refusal = self.guard.check(recipients)
        if refusal is not None:
            logger.warning("reply %s refused by the send guard: %s", job_id, refusal)
            self._audit_job(job, "send_rejected", reason=refusal)
            self._send_result(job_id, _error("rejected", refusal, _GUARD_MESSAGES.get(refusal, refusal)))
            return

        try:
            message_id = self._send(self.settings.mail, job)
        except SmtpSendError as error:
            logger.warning("reply %s could not be sent: %s (%s)", job_id, error.category, error.code)
            self._audit_job(
                job, "send_failed", reason=f"{error.category} {error.code}".strip(), started=started
            )
            self._send_result(job_id, _error(error.category, error.code, str(error)))
            return

        self.guard.record_send(recipients[0])
        self._audit_job(job, "sent", message_id=message_id, started=started)
        self._send_result(
            job_id, {"status": "ok", "sentMessageId": message_id, "durationMs": _elapsed_ms(started)}
        )

    def _run_test_connection(self, job: dict[str, Any], started: float) -> None:
        job_id = str(job["jobId"])
        try:
            report = self._test_mailbox(self.settings)
        except (mailbox_module.MailboxError, SmtpSendError) as error:
            code = getattr(error, "code", "")
            self._send_result(job_id, _error(error.category, code, str(error)))
            return
        self._send_result(job_id, {"status": "ok", "durationMs": _elapsed_ms(started), **report})

    def _audit_job(
        self,
        job: dict[str, Any],
        status: str,
        *,
        reason: str | None = None,
        message_id: str | None = None,
        started: float | None = None,
    ) -> None:
        self.audit.record(
            status=status,
            job_id=str(job.get("jobId")),
            message_id=message_id,
            sender=self.settings.mail.address,
            to=[str(address) for address in job.get("to") or []],
            subject=str(job.get("subject") or ""),
            agent_id=job.get("agentId"),
            conversation_id=job.get("conversationId"),
            reason=reason,
            duration_ms=_elapsed_ms(started) if started is not None else None,
        )

    def _send_result(self, job_id: str, payload: dict[str, Any]) -> None:
        path = RESULT_PATH.format(job_id=job_id)
        delay = 0.5

        for attempt in range(1, RESULT_ATTEMPTS + 1):
            try:
                self.client.post(path, payload, timeout=30.0)
                return
            except QlarRejected as rejection:
                logger.warning("result for job %s refused: %s", job_id, rejection)
                return
            except QlarUnreachable as error:
                if attempt == RESULT_ATTEMPTS:
                    logger.error("giving up sending result for job %s: %s", job_id, error)
                    return
                self._sleep_with_jitter(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    def _current_in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def _sleep_with_jitter(self, seconds: float) -> None:
        # Jitter so that a fleet of gateways reconnecting after a Qlar deployment does not arrive
        # as one synchronised wave.
        self._stopping.wait(seconds * (0.7 + random.random() * 0.6))  # noqa: S311


_GUARD_MESSAGES = {
    "no_recipient": "the job names no recipient",
    "too_many_recipients": "a reply may have only one recipient",
    "invalid_recipient": "the recipient is not a single plain email address",
    "recipient_not_known": "the recipient has not written to this mailbox recently and is not allow-listed",
    "rate_limited": "too many replies to this recipient in the last hour",
}


def _error(category: str, code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"category": category, "code": code, "messageText": message}}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _is_expired(expires_at: Any) -> bool:
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) > deadline
