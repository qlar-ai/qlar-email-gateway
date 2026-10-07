"""The customer's own record of every email the gateway forwarded, filtered or sent (PRD FR-43).

This is not a debugging log. For many organisations it is the reason they accept the gateway at
all: "we want to see, in our own systems, every email that went to Qlar and every reply that came
back". The file is theirs, it never leaves the premises, and nothing in the protocol can turn it
off. The mailbox password is never written here.

One JSON object per line, so it can be tailed, grepped, or shipped to a SIEM without a parser.
Rotation is by size and kept simple deliberately — a logging framework here would be another
dependency in someone else's network for very little gain.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 32 * 1024 * 1024
KEEP_FILES = 5
SUBJECT_LIMIT = 200

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(
        self,
        *,
        status: str,
        message_id: str | None = None,
        sender: str | None = None,
        to: list[str] | str | None = None,
        subject: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        reason: str | None = None,
        duration_ms: int | None = None,
        job_id: str | None = None,
        uid: int | None = None,
    ) -> None:
        """Appends one entry. Never raises — auditing must not stop mail flowing.

        `status` is what happened: `forwarded`, `filtered`, `rejected_by_qlar`,
        `uidvalidity_reset`, `sent`, `send_rejected`, `send_failed`, `expired`, `bad_signature`.
        """
        if self.path is None:
            return

        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "status": status,
            "messageId": message_id,
            "from": sender,
            "to": to,
            "subject": (subject or "")[:SUBJECT_LIMIT] if subject is not None else None,
            "agentId": agent_id,
            "conversationId": conversation_id,
            "reason": reason,
            "durationMs": duration_ms,
            "jobId": job_id,
            "uid": uid,
        }

        try:
            with _lock:
                self._rotate_if_needed()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: S110 - deliberate best-effort write
            # A full disk or a permissions mistake must not stop the gateway; the operator sees it
            # in the process log instead.
            pass

    def _rotate_if_needed(self) -> None:
        if self.path is None or not self.path.exists():
            return
        if self.path.stat().st_size < MAX_BYTES:
            return

        oldest = self.path.with_suffix(self.path.suffix + f".{KEEP_FILES}")
        if oldest.exists():
            oldest.unlink()
        for index in range(KEEP_FILES - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            if source.exists():
                source.rename(self.path.with_suffix(self.path.suffix + f".{index + 1}"))
        os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
