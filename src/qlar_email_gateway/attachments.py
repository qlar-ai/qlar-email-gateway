"""Inbound attachments: which parts of an email are files, and handing them to Qlar (0.2.0).

Qlar hosts what the gateway uploads and answers with a URL; the email is then forwarded with
those URLs in `attachments[]`. The rules, all of which exist so that a refused or failed upload
costs one attachment and never the email:

* Only the **first five** attachments of an email, by position, are offered to Qlar. The rest are
  still listed (with an empty `url`) so the agent can say it saw them.
* A file over 20 MB, or of a type Qlar does not read, is listed without being uploaded.
* Any refusal or failure means "not uploaded": the attachment is listed with an empty `url`.
  The one exception is a revoked or deleted gateway, which must stop exactly as it does elsewhere.

File names and sender addresses are never logged here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import Message
from typing import Any, Protocol
from urllib.parse import quote

from .client import QlarRejected, QlarUnreachable, stop_reason

logger = logging.getLogger("qlar_email_gateway.attachments")

ATTACHMENTS_PATH = "/attachments"
#: Attachments beyond this many (by position in the email) are listed but never uploaded.
MAX_UPLOADED_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
UPLOAD_TIMEOUT_SECONDS = 120.0

_IMAGE_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md", ".jpg", ".jpeg", ".png", ".webp")


@dataclass
class Attachment:
    file_name: str
    content_type: str
    data: bytes


class _Uploader(Protocol):
    def post_bytes(
        self, path: str, body: bytes, content_type: str, *, timeout: float = ...
    ) -> tuple[int, dict[str, Any]]: ...


def collect_attachments(msg: Message) -> list[Attachment]:
    """The files of an email, in the order they appear.

    Parts marked `attachment`, plus `inline` parts that have a file name and no `Content-ID`.
    An inline part that other parts refer to by `cid:` (a signature logo, a picture in the body)
    is part of the message's look, not a file the sender sent, so it is ignored entirely.
    """
    found: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            continue
        disposition = part.get_content_disposition()
        raw_name = part.get_filename()
        if disposition == "attachment":
            pass
        elif disposition == "inline" and raw_name and not part.get("Content-ID"):
            pass
        else:
            continue

        data = _part_bytes(part)
        if data is None:
            continue
        found.append(
            Attachment(
                file_name=_clean_name(raw_name) or "attachment",
                content_type=part.get_content_type(),
                data=data,
            )
        )
    return found


def upload_all(
    client: _Uploader, message_id: str, sender: str, items: list[Attachment]
) -> list[dict[str, Any]]:
    """Uploads what is eligible and returns the `attachments[]` entries for `/inbound`.

    `message_id` must be exactly the string sent as `messageId` to `/inbound`: Qlar only accepts
    for an email the URLs it recorded under that id. Raises Revoked/Deleted when Qlar says so.
    """
    entries: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        url = ""
        if message_id and position < MAX_UPLOADED_ATTACHMENTS and _worth_uploading(item):
            url = _upload(client, message_id, sender, item, position)
        entries.append(
            {
                "fileName": item.file_name,
                "contentType": item.content_type,
                "sizeBytes": len(item.data),
                "url": url,
            }
        )
    return entries


def _upload(client: _Uploader, message_id: str, sender: str, item: Attachment, position: int) -> str:
    path = (
        f"{ATTACHMENTS_PATH}?messageId={_encode(message_id)}"
        f"&fileName={_encode(item.file_name)}&from={_encode(sender)}"
    )
    try:
        _, body = client.post_bytes(path, item.data, item.content_type, timeout=UPLOAD_TIMEOUT_SECONDS)
    except QlarRejected as rejection:
        if (stop := stop_reason(rejection)) is not None:
            raise stop from rejection
        logger.warning("attachment %d was not uploaded: HTTP %d", position + 1, rejection.status)
        return ""
    except QlarUnreachable as error:
        logger.warning(
            "attachment %d was not uploaded: Qlar unreachable (%s)", position + 1, type(error).__name__
        )
        return ""

    url = body.get("url")
    if body.get("enabled") is False or not isinstance(url, str) or not url:
        logger.info("attachment %d was refused by Qlar: %s", position + 1, body.get("reason") or "no url")
        return ""
    return url


def _worth_uploading(item: Attachment) -> bool:
    """A pre-filter that is never stricter than Qlar: it only saves sending bytes Qlar will refuse."""
    if len(item.data) > MAX_ATTACHMENT_BYTES:
        return False
    if item.content_type.lower() in _IMAGE_TYPES:
        return True
    return item.file_name.lower().endswith(_SUPPORTED_EXTENSIONS)


def _encode(value: str) -> str:
    """Percent-encodes everything but RFC 3986 unreserved characters (letters, digits, `-._~`)."""
    return quote(value, safe="")


def _clean_name(name: str | None) -> str:
    """The file name without any directory part (either separator) or control characters."""
    if not name:
        return ""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch for ch in base if ch.isprintable()).strip()


def _part_bytes(part: Message) -> bytes | None:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload(0) if part.is_multipart() else None
        return inner.as_bytes() if inner is not None else None
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else None
