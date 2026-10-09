"""The mailbox watcher: new mail in INBOX → `POST /inbound` (PRD FR-20, FR-21, FR-24, FR-40).

The rules this module exists to keep:

* **Old mail is never answered.** On first run, and whenever the server's `UIDVALIDITY` changes
  (the server renumbered the folder), the watcher starts from the next UID and forwards nothing
  that was already there.
* **Every new email is forwarded exactly once, in UID order.** `lastUid` is saved only after Qlar
  accepted the email (`202`), so a Qlar outage, a gateway restart or an hour offline just means
  the backlog is forwarded later. Qlar de-duplicates a re-sent Message-ID, so the one case that
  can repeat — a crash between the 202 and saving — costs nothing.
* **One bad email does not block the rest.** A `400` from Qlar means "this email can never be
  accepted": it is audited and skipped, not retried forever.
* **The mailbox is read, never changed.** The folder is opened read-only and messages are fetched
  with `BODY.PEEK[]`, so nothing is marked read, moved or deleted.

IDLE is used when the server offers it, one minute at a time with a sync after each. A
server that drops IDLE four times within five minutes is polled instead until the next restart.
"""

from __future__ import annotations

import imaplib
import logging
import random
import ssl
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from . import status as mailbox_status
from .audit import AuditLog
from .client import QlarRejected, QlarUnreachable, Revoked, stop_reason
from .config import EnrollmentState, MailSettings, Settings
from .extract import build_inbound_payload
from .filters import filter_reason
from .send_guard import SendGuard, utcnow
from .sender import probe_smtp
from .status import MailboxStatus

logger = logging.getLogger("qlar_email_gateway.mailbox")

INBOUND_PATH = "/inbound"
IMAP_TIMEOUT_SECONDS = 30
IDLE_CHECK_SECONDS = 60
#: An IDLE check that returns empty sooner than this did not time out: the connection closed.
IDLE_EARLY_RETURN = timedelta(seconds=IDLE_CHECK_SECONDS / 2)
LOGIN_RETRY_SECONDS = 60
APPROVAL_WAIT_SECONDS = 4
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 15.0
QLAR_RETRY_MAX_SECONDS = 60.0
IDLE_DROP_LIMIT = 4
IDLE_DROP_WINDOW = timedelta(minutes=5)

#: What a dropped or broken IMAP connection looks like, from the socket up to the protocol.
CONNECTION_ERRORS = (OSError, imaplib.IMAP4.error, imaplib.IMAP4.abort)


def _is_bye(item: Any) -> bool:
    return isinstance(item, tuple) and len(item) > 0 and item[0] == b"BYE"


class MailboxError(Exception):
    """The mailbox test failed. `category` is one of the protocol's result categories."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def default_imap_factory(mail: MailSettings) -> IMAPClient:
    """Opens the IMAP connection, TLS from the start (`ssl`) or upgraded (`starttls`)."""
    context = ssl.create_default_context()
    if mail.imap_security == "ssl":
        return IMAPClient(
            mail.imap_host, port=mail.imap_port, ssl=True, ssl_context=context, timeout=IMAP_TIMEOUT_SECONDS
        )
    client = IMAPClient(mail.imap_host, port=mail.imap_port, ssl=False, timeout=IMAP_TIMEOUT_SECONDS)
    client.starttls(context)
    return client


def test_mailbox(
    settings: Settings, imap_factory: Callable[[MailSettings], Any] = default_imap_factory
) -> dict[str, Any]:
    """Logs in to IMAP and SMTP once and reports what it found (`test-mailbox`, `test_connection`).

    Raises MailboxError (IMAP) or SmtpSendError (SMTP) with a category on failure. Never contacts Qlar.
    """
    mail = settings.mail
    try:
        client = imap_factory(mail)
        try:
            welcome = getattr(client, "welcome", b"") or b""
            client.login(mail.user, mail.password)
            capabilities = client.capabilities()
            selected = client.select_folder(mail.imap_folder, readonly=True)
        finally:
            try:
                client.logout()
            except Exception:  # noqa: BLE001, S110 - best effort on the way out
                pass
    except LoginError as error:
        raise MailboxError("auth", "IMAP login refused") from error
    except TimeoutError as error:
        raise MailboxError("timeout", "the IMAP server did not answer in time") from error
    except CONNECTION_ERRORS as error:
        raise MailboxError(
            "connection", f"could not reach the IMAP server: {type(error).__name__}"
        ) from error

    smtp_banner = probe_smtp(mail)

    return {
        "imapBanner": welcome.decode("utf-8", errors="replace")
        if isinstance(welcome, bytes)
        else str(welcome),
        "smtpBanner": smtp_banner,
        "idleSupported": b"IDLE" in tuple(capabilities),
        "inboxCount": int(selected.get(b"EXISTS", 0)),
    }


class MailboxWatcher:
    def __init__(
        self,
        settings: Settings,
        state: EnrollmentState,
        client: Any,
        audit: AuditLog,
        guard: SendGuard,
        status: MailboxStatus,
        imap_factory: Callable[[MailSettings], Any] = default_imap_factory,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.client = client
        self.audit = audit
        self.guard = guard
        self.status = status
        self.imap_factory = imap_factory
        self.clock = clock
        self._stopping = threading.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._idle_drops: deque[datetime] = deque()
        self._idle_disabled = False

    # -- lifecycle -----------------------------------------------------------------------------

    def stop(self) -> None:
        self._stopping.set()

    def run_forever(self) -> None:
        """Watches until stopped. Raises Revoked when Qlar says this gateway is revoked."""
        backoff = RECONNECT_MIN_SECONDS

        while not self._stopping.is_set():
            imap: Any = None
            in_idle = False
            try:
                imap = self.imap_factory(self.settings.mail)
                imap.login(self.settings.mail.user, self.settings.mail.password)
                uses_idle = b"IDLE" in tuple(imap.capabilities()) and not self._idle_disabled
                self._select(imap)
                self.status.set(mailbox_status.IDLE_UNSUPPORTED if self._idle_disabled else mailbox_status.OK)
                backoff = RECONNECT_MIN_SECONDS

                self._sync(imap)
                if uses_idle:
                    in_idle = True
                    self._idle_loop(imap)
                else:
                    self._poll_loop(imap)
            except Revoked:
                raise
            except LoginError:
                logger.error(
                    "the mail server refused the login for %s; retrying in %ds",
                    self.settings.mail.user,
                    LOGIN_RETRY_SECONDS,
                )
                self.status.set(mailbox_status.AUTH_FAILED)
                self._sleep(LOGIN_RETRY_SECONDS)
            except CONNECTION_ERRORS as error:
                if in_idle:
                    self._record_idle_drop()
                if not self._stopping.is_set():
                    logger.warning(
                        "mailbox connection lost (%s); reconnecting in %.0fs", type(error).__name__, backoff
                    )
                    self.status.set(mailbox_status.UNREACHABLE)
                    self._sleep(backoff + random.random() * 0.9)  # noqa: S311 - jitter, not security
                    backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)
            finally:
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:  # noqa: BLE001, S110 - the connection may already be gone
                        pass

    # -- connection modes ----------------------------------------------------------------------

    def _idle_loop(self, imap: Any) -> None:
        """One IDLE per minute, then always a sync.

        Re-entering IDLE after every check, and searching after every IDLE, closes two gaps an
        IDLE that is held open has: a new-mail notice that arrived while the watcher was busy
        forwarding (the server does not repeat it once IDLE starts again) is picked up at the next
        sync, and the server's 30-minute IDLE limit is never reached. The cost is one UID SEARCH a
        minute.

        imapclient returns an empty list at once — rather than raising — when the server closes
        the connection during IDLE, so a check that comes back well before its timeout with nothing
        in it, or with a BYE, is treated as the connection dropping.
        """
        while not self._stopping.is_set():
            imap.idle()
            started = self.clock()
            try:
                responses = imap.idle_check(timeout=IDLE_CHECK_SECONDS) or []
            finally:
                if not self._stopping.is_set():
                    imap.idle_done()
            if self._stopping.is_set():
                break

            if any(_is_bye(item) for item in responses):
                raise ConnectionError("the mail server ended the IDLE session (BYE)")
            if not responses and self.clock() - started < IDLE_EARLY_RETURN:
                raise ConnectionError("IDLE ended long before its timeout; the connection was closed")

            self._sync(imap)

        try:
            imap.idle_done()
        except Exception:  # noqa: BLE001, S110 - stopping; the server may have gone already
            pass

    def _poll_loop(self, imap: Any) -> None:
        while not self._stopping.is_set():
            imap.noop()
            self._sync(imap)
            self._sleep(self.settings.poll_interval_seconds)

    def _record_idle_drop(self) -> None:
        now = self.clock()
        self._idle_drops.append(now)
        while self._idle_drops and now - self._idle_drops[0] > IDLE_DROP_WINDOW:
            self._idle_drops.popleft()
        if len(self._idle_drops) >= IDLE_DROP_LIMIT and not self._idle_disabled:
            self._idle_disabled = True
            logger.warning(
                "the mail server dropped IDLE %d times in 5 minutes; checking every %ds until restart",
                IDLE_DROP_LIMIT,
                self.settings.poll_interval_seconds,
            )

    # -- mail ----------------------------------------------------------------------------------

    def _select(self, imap: Any) -> None:
        selected = imap.select_folder(self.settings.mail.imap_folder, readonly=True)
        uid_validity = int(selected.get(b"UIDVALIDITY", 0))
        uid_next = int(selected.get(b"UIDNEXT", 1))

        if self.state.last_uid is None or self.state.uid_validity != uid_validity:
            previous = self.state.uid_validity
            self.state.uid_validity = uid_validity
            self.state.last_uid = uid_next - 1
            self._save_state()
            self.audit.record(
                status="uidvalidity_reset",
                uid=self.state.last_uid,
                reason=f"uidvalidity {previous} -> {uid_validity}" if previous is not None else "first run",
            )
            logger.info(
                "watching %s from UID %d; earlier mail is not answered",
                self.settings.mail.imap_folder,
                uid_next,
            )

    def _sync(self, imap: Any) -> None:
        last_uid = self.state.last_uid or 0
        found = imap.search(["UID", f"{last_uid + 1}:*"])
        for uid in sorted(int(value) for value in found if int(value) > last_uid):
            if self._stopping.is_set():
                return
            self._process(imap, uid)

    def _process(self, imap: Any, uid: int) -> None:
        fetched = imap.fetch([uid], ["BODY.PEEK[]"])
        raw = (fetched.get(uid) or {}).get(b"BODY[]", b"")

        # Anyone on the internet can send this mailbox anything, and the standard library's header
        # parser raises on some malformed headers. One such email must cost one email, not the
        # gateway: it is audited and skipped, and the watcher moves on to the next.
        try:
            msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
            reason = filter_reason(msg, self.settings.mail.address, self._enrolled_at())
            description = self._describe(msg)
            payload = (
                None
                if reason is not None
                else build_inbound_payload(msg, uid, self.settings.max_inbound_text_chars)
            )
        except Exception as error:  # noqa: BLE001 - any parse failure is this email's problem only
            logger.warning("email UID %d could not be read (%s); skipped", uid, type(error).__name__)
            self.audit.record(status="unparseable", uid=uid, reason=type(error).__name__)
            self._advance(uid)
            return

        if payload is None:
            self.audit.record(status="filtered", reason=reason, uid=uid, **description)
            self._advance(uid)
            return

        if self._forward(payload):
            try:
                self.guard.remember_inbound([payload["from"]["address"], payload.get("replyTo")])
            except OSError as error:
                logger.error("could not record the sender for replies: %s", error)
            self.audit.record(status="forwarded", uid=uid, **description)
            self._advance(uid)
        elif not self._stopping.is_set():
            self.audit.record(status="rejected_by_qlar", uid=uid, **description)
            self._advance(uid)

    def _forward(self, payload: dict[str, Any]) -> bool:
        """POSTs until Qlar accepts (True) or refuses for good (False). Raises Revoked."""
        delay = 1.0
        while not self._stopping.is_set():
            try:
                self.client.post(INBOUND_PATH, payload, timeout=30.0)
                return True
            except QlarRejected as rejection:
                reason = str(rejection.body.get("reason", "")).lower()
                if rejection.status == 400:
                    logger.warning("Qlar refused email %s for good: %s", payload["messageId"], rejection)
                    return False
                if (stop := stop_reason(rejection)) is not None:
                    raise stop from rejection
                if rejection.status == 403 and reason == "pending_approval":
                    self._sleep(APPROVAL_WAIT_SECONDS)
                    continue
                logger.warning(
                    "Qlar refused email %s (%s); retrying in %.0fs", payload["messageId"], rejection, delay
                )
            except QlarUnreachable as error:
                logger.warning(
                    "Qlar unreachable (%s); retrying email %s in %.0fs", error, payload["messageId"], delay
                )
            self._sleep(delay)
            delay = min(delay * 2, QLAR_RETRY_MAX_SECONDS)
        return False

    def _advance(self, uid: int) -> None:
        self.state.last_uid = uid
        self._save_state()

    def _save_state(self) -> None:
        """Saves the state file; a failure is a disk problem, said as one, not a network one.

        Raising here would land in the connection handler, report the mailbox as unreachable and
        re-forward the same email in a loop while the operator looks at the network. The position
        is kept in memory and the next successful save catches up; Qlar de-duplicates anything
        re-sent after a restart.
        """
        try:
            self.state.save(self.settings.state_file)
        except OSError as error:
            logger.error(
                "could not write %s (%s); the gateway keeps working but will re-send recent mail after a "
                "restart until this is fixed",
                self.settings.state_file,
                error,
            )

    def _enrolled_at(self) -> datetime:
        try:
            enrolled = datetime.fromisoformat(self.state.enrolled_at)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)
        return enrolled if enrolled.tzinfo is not None else enrolled.replace(tzinfo=UTC)

    @staticmethod
    def _describe(msg: EmailMessage) -> dict[str, Any]:
        return {
            "message_id": str(msg.get("Message-ID") or "").strip() or None,
            "sender": str(msg.get("From") or "") or None,
            "to": str(msg.get("To") or "") or None,
            "subject": str(msg.get("Subject") or ""),
        }

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stopping.wait(seconds)
