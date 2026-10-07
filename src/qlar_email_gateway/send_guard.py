"""Who the gateway may email, decided on this machine (PRD FR-33).

Qlar signs every reply job, so a job that verifies really came from Qlar. That is not the same as
"this reply should be sent": a compromised or confused Qlar could still ask for mail to anyone.
The guard is the answer to that, and it lives here precisely so nothing Qlar sends can loosen it:

* exactly one recipient per reply;
* that recipient wrote to this mailbox through the gateway within `RECIPIENT_MEMORY_DAYS`
  (both their `From` and `Reply-To` are remembered), or is on `RECIPIENT_ALLOWLIST`;
* at most `MAX_SENDS_PER_HOUR_PER_RECIPIENT` replies to them in any hour.

`From` and "no attachments" are enforced by `sender.py`, which takes neither from the job.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from .config import STATE_LOCK, EnrollmentState, Settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class SendGuard:
    def __init__(
        self,
        settings: Settings,
        state: EnrollmentState,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.state = state
        self.clock = clock
        # Sends per recipient in the last hour. In memory on purpose: a restart forgets it, which
        # at worst allows one extra hour's worth of replies — not worth a disk write per send.
        self._sends: dict[str, deque[datetime]] = defaultdict(deque)
        self._sends_lock = threading.Lock()

    def remember_inbound(self, addresses: Iterable[str | None]) -> None:
        """Records that these addresses wrote in now, so they may be answered."""
        now = self.clock().isoformat()
        cleaned = {address.strip().lower() for address in addresses if address and address.strip()}
        if not cleaned:
            return
        with STATE_LOCK:
            for address in cleaned:
                self.state.recipients[address] = now
        self.state.save(self.settings.state_file)

    def check(self, to: list[str]) -> str | None:
        """None when a reply to `to` may be sent, otherwise the rejection code."""
        recipients = [address.strip().lower() for address in to if address and address.strip()]
        if not recipients:
            return "no_recipient"
        if len(recipients) > 1:
            return "too_many_recipients"

        recipient = recipients[0]
        if not self._is_allowlisted(recipient) and not self._wrote_in_recently(recipient):
            return "recipient_not_known"

        with self._sends_lock:
            recent = self._prune(recipient)
            if len(recent) >= self.settings.max_sends_per_hour_per_recipient:
                return "rate_limited"

        return None

    def record_send(self, to: str) -> None:
        """Counts one sent reply against the recipient's hourly limit."""
        recipient = to.strip().lower()
        with self._sends_lock:
            self._prune(recipient).append(self.clock())

    def _prune(self, recipient: str) -> deque[datetime]:
        window_start = self.clock() - timedelta(hours=1)
        recent = self._sends[recipient]
        while recent and recent[0] <= window_start:
            recent.popleft()
        return recent

    def _is_allowlisted(self, recipient: str) -> bool:
        domain = recipient.rpartition("@")[2]
        for entry in self.settings.recipient_allowlist:
            if entry == recipient:
                return True
            # "@corp.com" and "corp.com" both mean the whole domain, and only that domain:
            # x@evilcorp.com and x@sub.corp.com do not match.
            if "@" not in entry.lstrip("@") and entry.lstrip("@") == domain:
                return True
        return False

    def _wrote_in_recently(self, recipient: str) -> bool:
        with STATE_LOCK:
            last_seen = self.state.recipients.get(recipient)
        if not last_seen:
            return False
        try:
            seen_at = datetime.fromisoformat(last_seen)
        except ValueError:
            return False
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        return self.clock() - seen_at <= timedelta(days=self.settings.recipient_memory_days)
