"""The run loop: ask Qlar for work, do it, send the answer back.

Outbound only. The gateway opens a long-poll request that Qlar holds for up to ~25 seconds
and answers either with a job or with `204 No Content`; either way the gateway immediately
asks again. Nothing listens on a port, nothing needs a certificate, and nothing has to be
reachable from the internet.

Because there is no session, a Qlar restart is not an outage here: the hanging poll fails,
the loop backs off for a second or two and asks again. Enrolment and approval are stored
server-side and survive, so no human has to do anything.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .audit import AuditLog
from .client import QlarClient, QlarNotAnEndpoint, QlarRejected, QlarUnreachable
from .config import EnrollmentState, Settings
from .crypto import load_or_create_private_key, verify_job

logger = logging.getLogger("qlar_email_gateway.poll")

POLL_PATH = "/jobs/poll"
RESULT_PATH = "/jobs/{job_id}/result"

MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

# Waiting to be approved is not a failure, so it does not use the error backoff. A human is
# looking at the screen in Qlar with the fingerprint in front of them; the gateway should start
# working within a few seconds of the click, not up to fifteen.
APPROVAL_POLL_SECONDS = 4.0

# How many times a finished result is re-sent if the network drops on the way back.
# Posting a result is idempotent on Qlar's side (keyed by job id), so a retry is safe and
# is much better than throwing away work the database already paid for.
RESULT_ATTEMPTS = 4


class Revoked(Exception):
    """Qlar says this gateway is revoked. The loop stops; a human must re-enrol it."""


class AwaitingApproval(Exception):
    """Enrolled, but no human has confirmed the fingerprint yet. The loop waits, quietly.

    This is the normal state of a gateway for the minute or two between installing it and
    someone clicking Approve, and the loop has always survived it — but it used to arrive as a
    nameless `QlarRejected`, logged as `Qlar rejected the poll: HTTP 403: Forbidden` every few
    seconds with the word "approval" nowhere in sight. Operators read that as a failure, killed
    the process, and then had to be told to start it again after approving.
    """


class PollLoop:
    def __init__(self, settings: Settings, state: EnrollmentState) -> None:
        self.settings = settings
        self.state = state
        private_key, _ = load_or_create_private_key(settings.key_file)
        self.client = QlarClient(
            base_url=settings.base_url,
            private_key=private_key,
            gateway_id=state.gateway_id,
            verify_tls=settings.verify_tls,
        )
        self.audit = AuditLog(settings.audit_log_file)
        self._stopping = threading.Event()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        # Reported on every poll so the CMS can show the data source as reachable or not
        # without waiting for someone to ask a question that fails.
        self._db_status = "unknown"
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.max_concurrent_queries),
            thread_name_prefix="qlar-job",
        )

    def stop(self) -> None:
        self._stopping.set()

    def run_forever(self) -> None:
        """Polls until stopped or revoked."""
        backoff = MIN_BACKOFF_SECONDS
        # Whether the "waiting for approval" line has already been said. It doubles as the flag
        # that makes the first successful poll announce the approval.
        announced_waiting = False
        logger.info(
            "gateway %s starting, polling %s (protocol %d, version %s)",
            self.state.gateway_id, self.settings.base_url, PROTOCOL_VERSION, __version__,
        )

        while not self._stopping.is_set():
            # Saturated: every worker is busy, so asking for more work would only let jobs
            # queue up locally and time out. Wait for a slot instead — Qlar will hold the
            # job and hand it to the next poll.
            if self._current_in_flight() >= self.settings.max_concurrent_queries:
                time.sleep(0.25)
                continue

            try:
                job = self._poll_once()
                backoff = MIN_BACKOFF_SECONDS
                if announced_waiting:
                    # The click happened. Say so plainly: this is the line that tells the
                    # operator the gateway is theirs and working, and that they are done.
                    logger.info("approved. Serving queries for %s", self.settings.base_url)
                    announced_waiting = False
                if job is not None:
                    self._dispatch(job)
            except AwaitingApproval:
                # Said once, not every few seconds: this wait is expected and is measured in
                # the time it takes a person to look at a fingerprint.
                if not announced_waiting:
                    logger.info(
                        "enrolled, waiting for approval in Qlar. Compare the key fingerprint "
                        "above with the one the CMS shows and click Approve; this starts "
                        "working on its own, nothing else to run here.",
                    )
                    announced_waiting = True
                self._sleep_with_jitter(APPROVAL_POLL_SECONDS)
            except Revoked:
                logger.error(
                    "this gateway has been revoked in the Qlar CMS; stopping. "
                    "Delete %s and enrol again to reconnect.", self.settings.state_file,
                )
                break
            except QlarUnreachable as error:
                logger.warning("Qlar unreachable (%s); retrying in %.1fs", error, backoff)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarNotAnEndpoint as wrong_address:
                # Retried like any other failure — the address may be a proxy having a bad
                # day — but named for what it is, because "rejected the poll" every 15
                # seconds is not a clue anyone can act on.
                logger.error(
                    "%s. That is not Qlar's API: check QLAR_BASE_URL (%s), which must end in "
                    "/api/email-gateway. Retrying in %.1fs",
                    wrong_address, self.settings.base_url, backoff,
                )
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarRejected as rejection:
                logger.error("Qlar rejected the poll: %s", rejection)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        self._pool.shutdown(wait=True, cancel_futures=False)
        logger.info("gateway stopped")

    def _poll_once(self) -> dict[str, Any] | None:
        payload = {
            "gatewayId": self.state.gateway_id,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "dbStatus": self._db_status,
            "maxWaitSeconds": self.settings.poll_timeout_seconds,
        }

        try:
            status, body = self.client.post(
                POLL_PATH,
                payload,
                # Comfortably longer than the server's hold, so a normal empty poll is not
                # mistaken for a network failure and does not trigger backoff.
                timeout=self.settings.poll_timeout_seconds + 15,
            )
        except QlarRejected as rejection:
            if rejection.status == 403:
                reason = str(rejection.body.get("reason", "")).lower()
                if reason == "revoked":
                    raise Revoked from rejection
                if reason == "pending_approval":
                    raise AwaitingApproval from rejection
            raise

        if status == 204 or not body:
            return None

        job = body.get("job") if "job" in body else body
        if not isinstance(job, dict) or not job.get("jobId"):
            return None

        # The job is signed by Qlar itself. Verifying it here means a compromised proxy at
        # the customer's own edge cannot inject SQL, even though it terminates the TLS.
        if not verify_job(self.state.qlar_public_key_pem, job):
            logger.error("job %s failed signature verification and was discarded", job.get("jobId"))
            return None

        if int(job.get("protocol", PROTOCOL_VERSION)) > PROTOCOL_VERSION:
            logger.error(
                "job %s needs protocol %s but this gateway speaks %d - upgrade the gateway",
                job.get("jobId"), job.get("protocol"), PROTOCOL_VERSION,
            )
            self._send_result(
                str(job["jobId"]),
                {
                    "status": "error",
                    "error": {
                        "category": "rejected",
                        "driverCode": None,
                        "messageText": f"gateway speaks protocol {PROTOCOL_VERSION}; job requires "
                                       f"{job.get('protocol')}. Upgrade the on-premise gateway.",
                        "hint": None,
                        "position": None,
                    },
                    "durationMs": 0,
                },
            )
            return None

        return job

    def _dispatch(self, job: dict[str, Any]) -> None:
        with self._in_flight_lock:
            self._in_flight += 1
        self._pool.submit(self._run_job, job)

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("jobId"))
        job_type = str(job.get("type", "execute_query"))
        sql = str(job.get("sql") or "")

        try:
            expires_at = job.get("expiresAt")
            if _is_expired(expires_at):
                # Qlar has already given up waiting; running it would cost the customer's
                # database for an answer nobody will read.
                logger.info("job %s expired before it started; skipping", job_id)
                self._send_result(job_id, {
                    "status": "error",
                    "error": {
                        "category": "expired", "driverCode": None,
                        "messageText": "job expired before execution", "hint": None, "position": None,
                    },
                    "durationMs": 0,
                })
                return

            # Replaced in the email build: send_email / test_connection dispatch lands with the
            # mailbox modules. Until then every job is refused rather than guessed at.
            self._send_result(job_id, {
                "status": "error",
                "error": {"category": "rejected", "code": "not_implemented",
                          "messageText": f"job type {job_type} is not handled yet"},
                "durationMs": 0,
            })

        except Exception as error:  # noqa: BLE001 - a worker must never die silently
            logger.exception("job %s failed unexpectedly", job_id)
            self._send_result(job_id, {
                "status": "error",
                "error": {
                    "category": "sql_error", "driverCode": None,
                    "messageText": f"{type(error).__name__}: {error}", "hint": None, "position": None,
                },
                "durationMs": 0,
            })
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def _send_result(self, job_id: str, payload: dict[str, Any]) -> None:
        path = RESULT_PATH.format(job_id=job_id)
        delay = 0.5

        for attempt in range(1, RESULT_ATTEMPTS + 1):
            try:
                self.client.post(path, payload, timeout=30.0)
                return
            except QlarRejected as rejection:
                # Qlar answered and said no: the job is gone or already answered. Retrying
                # would not change that.
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
        # Jitter so that a fleet of gateways reconnecting after a Qlar deployment does not
        # arrive as one synchronised wave.
        self._stopping.wait(seconds * (0.7 + random.random() * 0.6))  # noqa: S311


def _is_expired(expires_at: Any) -> bool:
    if not expires_at:
        return False
    from datetime import datetime

    try:
        deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) > deadline
