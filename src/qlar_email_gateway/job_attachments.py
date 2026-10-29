"""Fetching the pictures a protocol-2 reply job declares (0.2.0).

The signed job carries only a description of each attachment (name, type, size, SHA-256, content
id); the bytes are fetched with a signed `GET /jobs/{jobId}/attachments/{index}` and accepted only
if their length and hash match what the signature covers. Anything that goes wrong means "send the
reply without pictures": the function returns None, never a partial list, and never raises for a
problem with the attachments themselves. A revoked or deleted gateway still stops, as elsewhere.

File names are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .client import QlarRejected, QlarUnreachable, stop_reason

logger = logging.getLogger("qlar_email_gateway.job_attachments")

MAX_ENTRIES = 5
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30.0

# What a job's fields must look like before they go into a URL path or a mail header. Anything
# else is not something Qlar produces, so the whole list is dropped rather than trusted.
_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")
_CONTENT_TYPE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
_CONTENT_ID = re.compile(r"^[0-9a-f]{32}@qlar$")


@dataclass
class Fetched:
    file_name: str
    content_type: str
    content_id: str | None
    data: bytes


class _Getter(Protocol):
    def get_bytes(self, path: str, *, max_bytes: int, timeout: float = ...) -> bytes: ...


def fetch_job_attachments(client: _Getter, job: dict[str, Any]) -> list[Fetched] | None:
    """The verified attachments of a job, or None when any of them cannot be had."""
    entries = job.get("attachments")
    if not isinstance(entries, list) or not entries:
        return []
    job_id = job.get("jobId")
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        logger.warning("a job with attachments has an unexpected id; they are skipped")
        return None
    if len(entries) > MAX_ENTRIES:
        logger.warning("job %s declares %d attachments; at most %d", job_id, len(entries), MAX_ENTRIES)
        return None

    declared = [_parse_entry(entry) for entry in entries]
    items = [item for item in declared if item is not None]
    if len(items) != len(declared):
        logger.warning("job %s declares an attachment that cannot be read", job_id)
        return None
    sizes = [item[2] for item in items]
    if any(size > MAX_ATTACHMENT_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
        logger.warning("job %s declares attachments over the size limits", job_id)
        return None

    fetched: list[Fetched] = []
    for index, (file_name, content_type, size, digest, content_id) in enumerate(items):
        try:
            data = client.get_bytes(
                f"/jobs/{job_id}/attachments/{index}",
                max_bytes=min(MAX_ATTACHMENT_BYTES, size),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
        except QlarRejected as rejection:
            if (stop := stop_reason(rejection)) is not None:
                raise stop from rejection
            logger.warning("attachment %d of job %s: HTTP %d", index + 1, job_id, rejection.status)
            return None
        except QlarUnreachable as error:
            logger.warning("attachment %d of job %s not fetched: %s", index + 1, job_id, type(error).__name__)
            return None

        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            logger.warning("attachment %d of job %s does not match its signed description", index + 1, job_id)
            return None
        fetched.append(Fetched(file_name, content_type, content_id, data))
    return fetched


def _parse_entry(entry: Any) -> tuple[str, str, int, str, str | None] | None:
    if not isinstance(entry, dict):
        return None
    file_name, content_type = entry.get("fileName"), entry.get("contentType")
    size, digest, content_id = entry.get("sizeBytes"), entry.get("sha256"), entry.get("contentId")
    if not isinstance(file_name, str) or not _FILE_NAME.fullmatch(file_name):
        return None
    if not isinstance(content_type, str) or not _CONTENT_TYPE.fullmatch(content_type):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    # Lower-case hex only: the signed line carries this string verbatim, and a comparison that
    # accepted another spelling would accept bytes the signature did not describe.
    if not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower():
        return None
    if content_id is not None and (not isinstance(content_id, str) or not _is_content_id(content_id)):
        return None
    return file_name, content_type, size, digest, content_id or None


def _is_content_id(value: str) -> bool:
    return value == "" or _CONTENT_ID.fullmatch(value) is not None
