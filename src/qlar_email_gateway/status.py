"""The mailbox health the gateway reports to Qlar on every poll (PRD FR-40).

Written by the mailbox watcher, read by the poll loop, so it is the one piece of mutable state the
two threads share besides the state file.
"""

from __future__ import annotations

import threading

#: The values Qlar understands. `unknown` is only ever the starting value.
OK = "ok"
AUTH_FAILED = "auth_failed"
UNREACHABLE = "unreachable"
IDLE_UNSUPPORTED = "idle_unsupported"
UNKNOWN = "unknown"


class MailboxStatus:
    def __init__(self) -> None:
        self._value = UNKNOWN
        self._lock = threading.Lock()

    def set(self, value: str) -> None:
        with self._lock:
            self._value = value

    def get(self) -> str:
        with self._lock:
            return self._value
