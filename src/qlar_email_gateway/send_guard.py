"""What the gateway checks before it sends a reply, decided on this machine (PRD FR-33).

Qlar signs every reply job, so a job that verifies really came from Qlar. The guard keeps each
send well-formed and slow, whatever the job says:

* exactly one recipient per reply, as one bare address (no display name, list or header tricks);
* at most `MAX_SENDS_PER_HOUR_PER_RECIPIENT` replies to them in any hour.

Who the agent may answer is decided in Qlar (the channel's public / private mode and its contact
lists). Until 0.1.3 the gateway also refused anyone who had not written to the mailbox in the last
30 days unless they were on `RECIPIENT_ALLOWLIST`; that rule was removed because it could only be
changed by editing `.env`, which the people running the gateway are not expected to do.

`From` and "no attachments" are enforced by `sender.py`, which takes neither from the job.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from .config import EnrollmentState, Settings

#: One bare addr-spec and nothing else: no display name, brackets, separators or whitespace.
_BARE_ADDRESS = re.compile(r"^[^@\s<>,;\"()]+@[^@\s<>,;\"()]+$")


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

    def check(self, to: list[str]) -> str | None:
        """None when a reply to `to` may be sent, otherwise the rejection code."""
        recipients = [address.strip().lower() for address in to if address and address.strip()]
        if not recipients:
            return "no_recipient"
        if len(recipients) > 1:
            return "too_many_recipients"
        # The raw string is what SMTP will be given, so it must be exactly one address. A value like
        # "x@evil.com, y@corp.com" would otherwise reach two people from a job that names one.
        raw = next(address for address in to if address and address.strip())
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw) or not _is_bare_address(
            recipients[0]
        ):
            return "invalid_recipient"

        recipient = recipients[0]
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


def _is_bare_address(value: str) -> bool:
    if not _BARE_ADDRESS.match(value):
        return False
    name, address = parseaddr(value)
    return not name and address == value
