"""One hosted mailbox: its settings, its enrolment, and the two loops that serve it.

The existing `PollLoop` and `MailboxWatcher` are reused unchanged. What differs from the on-prem
`cli` is everything around them: settings are built from the control-plane payload and the vault
secret instead of the environment, every file lives under `{state_dir}/{gatewayId}/`, enrolment
never prompts, and the job pool is shared by all mailboxes of the process.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..client import Revoked
from ..config import DEFAULT_POLL_TIMEOUT_SECONDS, EnrollmentState, MailSettings, Settings
from ..enroll import EnrollmentCodeRejected, EnrollmentError, enroll
from ..mailbox import MailboxWatcher
from ..poll import PollLoop
from ..status import MailboxStatus
from .api import ControlPlaneClient

logger = logging.getLogger("qlar_email_gateway.hosted.runtime")

#: Consecutive login failures walk this schedule (seconds), so a changed password does not get the
#: customer's account locked by a retry every minute (FR-H13).
LOGIN_RETRY_SCHEDULE = (60, 120, 300, 900)
JOIN_TIMEOUT_SECONDS = 10.0


def build_settings(
    gateway_id: str,
    base_url: str,
    state_dir: Path,
    mail: MailSettings,
    enrollment_code: str | None,
) -> Settings:
    folder = state_dir / gateway_id
    return Settings(
        base_url=base_url,
        gateway_name=f"hosted:{mail.address}",
        enrollment_code=enrollment_code,
        key_file=folder / "gateway-key.pem",
        state_file=folder / "gateway-state.json",
        audit_log_file=folder / "audit" / "mail.jsonl",
        poll_timeout_seconds=DEFAULT_POLL_TIMEOUT_SECONDS,
        verify_tls=True,
        mail=mail,
    )


class GatewayAuditLog(AuditLog):
    """An audit log that stamps every entry with the hosted gateway it belongs to.

    All mailboxes of the worker share the module-wide write lock, so the entries stay whole; the id
    is what lets one file's worth of lines be told apart when they are shipped together.
    """

    def __init__(self, path: Path | None, gateway_id: str) -> None:
        super().__init__(path)
        self.gateway_id = gateway_id

    def record(self, **kwargs: Any) -> None:
        kwargs.setdefault("gateway_id", self.gateway_id)
        super().record(**kwargs)


class MailboxRuntime:
    def __init__(
        self,
        registration: dict[str, Any],
        mail: MailSettings,
        base_url: str,
        state_dir: Path,
        control: ControlPlaneClient,
        executor: ThreadPoolExecutor,
        *,
        enroll_fn: Callable[..., tuple[EnrollmentState, str]] = enroll,
    ) -> None:
        self.registration = registration
        self.gateway_id = str(registration["gatewayId"])
        self.mail: MailSettings | None = mail
        self.settings: Settings | None = build_settings(
            self.gateway_id, base_url, state_dir, mail, None
        )
        self._base_url = base_url
        self._state_dir = state_dir
        self._control = control
        self._executor = executor
        self._enroll = enroll_fn
        self.state: EnrollmentState | None = None
        self._loop: PollLoop | None = None
        self._watcher: MailboxWatcher | None = None
        self._threads: list[threading.Thread] = []

    # -- enrolment -----------------------------------------------------------------------------

    def ensure_enrolled(self) -> None:
        settings = self._require_settings()

        existing = EnrollmentState.load(settings.state_file)
        if existing is not None and settings.key_file.exists():
            self.state = existing
            return

        if self.registration.get("enrolled"):
            # Qlar holds a key for this registration that this machine no longer has (state wiped,
            # or a crash between redeeming the code and saving). Only a rekey makes it enrollable.
            logger.warning("mailbox %s is enrolled at Qlar but has no local key; rekeying", self.gateway_id)
            code: str | None = self._control.rekey(self.gateway_id)
        else:
            code = self.registration.get("enrollmentCode")
        if not code:
            raise EnrollmentError(f"no enrolment code is available for {self.gateway_id}")

        try:
            self.state = self._redeem(code)
        except EnrollmentCodeRejected:
            # The list this runtime came from may be older than the code Qlar now holds. One look
            # at the current list, one more try; a second refusal is a real problem.
            fresh = self._fresh_code()
            if not fresh or fresh == code:
                raise
            logger.info("enrolment code for %s was stale; retrying with a fresh one", self.gateway_id)
            self.state = self._redeem(fresh)

    def _redeem(self, code: str) -> EnrollmentState:
        settings = self._require_settings()
        replaced = replace(settings, enrollment_code=code)
        state, _ = self._enroll(replaced, replace_existing=True)
        return state

    def _fresh_code(self) -> str | None:
        for row in self._control.list_registrations():
            if str(row.get("gatewayId")) == self.gateway_id:
                return row.get("enrollmentCode") or None
        return None

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        settings = self._require_settings()
        if self.state is None:
            raise EnrollmentError(f"{self.gateway_id} is not enrolled; call ensure_enrolled first")

        status = MailboxStatus()
        audit = GatewayAuditLog(settings.audit_log_file, self.gateway_id)
        loop = PollLoop(settings, self.state, status=status, audit=audit, executor=self._executor)
        watcher = MailboxWatcher(
            settings,
            self.state,
            loop.client,
            audit,
            loop.guard,
            status,
            login_retry_schedule=LOGIN_RETRY_SCHEDULE,
        )
        self._loop, self._watcher = loop, watcher

        self._threads = [
            threading.Thread(target=self._watch, name=f"qlar-mailbox:{self.gateway_id}", daemon=True),
            threading.Thread(target=self._poll, name=f"qlar-poll:{self.gateway_id}", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self._loop is not None:
            self._loop.stop()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        # Credentials are only ever held for as long as the mailbox is served.
        self.mail = None
        self.settings = None

    @property
    def is_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    @property
    def stopped_by(self) -> str | None:
        stop = self._loop.stopped_by if self._loop is not None else None
        return None if stop is None else type(stop).__name__.lower()

    # -- thread bodies -------------------------------------------------------------------------

    def _watch(self) -> None:
        assert self._watcher is not None and self._loop is not None  # noqa: S101
        try:
            self._watcher.run_forever()
        except Revoked as stop:
            logger.error(
                "mailbox %s: Qlar says the gateway is %s; stopping",
                self.gateway_id,
                type(stop).__name__.lower(),
            )
            if self._loop.stopped_by is None:
                self._loop.stopped_by = stop
        except Exception:
            # A bug, not a mail-server problem (those are retried inside the watcher). Stop this
            # mailbox only; the controller sees it is no longer alive and starts it again.
            logger.exception("mailbox %s: the watcher stopped unexpectedly", self.gateway_id)
        self._loop.stop()

    def _poll(self) -> None:
        assert self._watcher is not None and self._loop is not None  # noqa: S101
        try:
            self._loop.run_forever()
        except Exception:
            logger.exception("mailbox %s: the job loop stopped unexpectedly", self.gateway_id)
        self._watcher.stop()

    def _require_settings(self) -> Settings:
        if self.settings is None:
            raise EnrollmentError(f"{self.gateway_id} has been stopped")
        return self.settings
