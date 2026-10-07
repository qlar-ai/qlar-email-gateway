"""The customer's own record of every query Qlar ran.

This is not a debugging log. For many organisations it is the actual reason the gateway
exists: "no open database port" usually also means "we want to see, in our own systems,
every statement that touched our data". The file is theirs, it never leaves the premises,
and nothing in the protocol can turn it off.

One JSON object per line, so it can be tailed, grepped, or shipped to a SIEM without a
parser. Rotation is by size and kept simple deliberately — a logging framework here would
be another dependency in someone else's network for very little gain.
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

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(
        self,
        *,
        job_id: str,
        job_type: str,
        sql: str,
        outcome: str,
        row_count: int,
        duration_ms: int,
        agent_id: str | None = None,
        user_id: str | None = None,
        conversation_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Appends one entry. Never raises — auditing must not break query serving."""
        if self.path is None:
            return

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "jobId": job_id,
            "jobType": job_type,
            "outcome": outcome,
            "agentId": agent_id,
            "userId": user_id,
            "conversationId": conversation_id,
            "rowCount": row_count,
            "durationMs": duration_ms,
            # The statement verbatim. Truncating it would defeat the point: an auditor
            # needs to see exactly what ran, not a summary of it.
            "sql": sql,
            "error": error,
        }

        try:
            with _lock:
                self._rotate_if_needed()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: S110 - deliberate best-effort cleanup
            # A full disk or a permissions mistake must not stop the gateway answering
            # queries; the operator sees it in the process log instead.
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
