"""Inbound attachments: which parts of an email are files, and handing them to Qlar (0.2.0).

Qlar hosts what the gateway uploads and answers with a URL; the email is then forwarded with
those URLs in `attachments[]`. The rules, all of which exist so that a refused or failed upload
costs one attachment and never the email:

* Only the **first five** attachments of an email, by position, are offered to Qlar. The rest are
  still listed (with an empty `url`) so the agent can say it saw them.
* A file over 20 MB, or of a type Qlar does not read, is listed without being uploaded.
* Any refusal or failure means "not uploaded": the attachment is listed with an empty `url`.
  A failed upload is not retried. The one exception is a revoked or deleted gateway, which must
  stop exactly as it does elsewhere.
* When Qlar is unreachable, or the gateway still waits for approval, the remaining files of that
  email are not tried: they would fail the same way, each after its own timeout.

File names and sender addresses are never logged here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .client import QlarRejected, QlarUnreachable, stop_reason

logger = logging.getLogger("qlar_email_gateway.attachments")

ATTACHMENTS_PATH = "/attachments"
#: Attachments beyond this many (by position in the email) are listed but never uploaded.
MAX_UPLOADED_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_NAME_CHARS = 255
UPLOAD_TIMEOUT_SECONDS = 120.0

_IMAGE_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})
_SUPPORTED_EXTENSIONS = (
    ".pdf",
    ".txt",
    ".md",
    ".docx",
    ".xlsx",
    ".csv",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)


@dataclass
class Attachment:
    """One file of an email. `data` is None when the bytes were deliberately not decoded.

    `size` is then an estimate (the encoded length, scaled for base64); it only ever fills the
    `sizeBytes` of a file that is listed but not uploaded. When `data` is present, the size is exact.
    """

    file_name: str
    content_type: str
    data: bytes | None
    size: int | None = None

    @property
    def size_bytes(self) -> int:
        if self.size is not None:
            return self.size
        return len(self.data) if self.data is not None else 0


class _Uploader(Protocol):
    def post_bytes(
        self, path: str, body: bytes, content_type: str, *, timeout: float = ...
    ) -> tuple[int, dict[str, Any]]: ...


def collect_attachments(msg: Message) -> list[Attachment]:
    """The files of an email, in the order they appear.

    Parts marked `attachment`, plus `inline` parts that have a file name and no `Content-ID`.
    An inline part that other parts refer to by `cid:` (a signature logo, a picture in the body)
    is part of the message's look, not a file the sender sent, so it is ignored entirely. An
    attached email (`message/rfc822`) is neither collected nor looked into.

    Decoding is lazy: only a file that can actually be uploaded (one of the first five, a type
    Qlar reads, not over 20 MB) is decoded. Every other file is listed with an estimated size and
    its bytes are never held. A part that cannot be read costs that one attachment, never the email.
    """
    found: list[Attachment] = []
    for part in _walk(msg):
        file_name = ""
        try:
            if part.is_multipart() or part.get_content_type() == "message/rfc822":
                continue
            disposition = part.get_content_disposition()
            raw_name = part.get_filename()
            if disposition == "attachment":
                pass
            elif disposition == "inline" and raw_name and not part.get("Content-ID"):
                pass
            else:
                continue
            file_name = _clean_name(raw_name) or "attachment"
            found.append(_collect_part(part, file_name, len(found)))
        except Exception as error:  # noqa: BLE001 - one unreadable part must not cost the email
            logger.warning("attachment %d could not be read (%s)", len(found) + 1, type(error).__name__)
            if file_name:
                found.append(Attachment(file_name, "application/octet-stream", None, 0))
    return found


def upload_all(
    client: _Uploader, message_id: str, sender: str, items: list[Attachment]
) -> list[dict[str, Any]]:
    """Uploads what is eligible and returns the `attachments[]` entries for `/inbound`.

    `message_id` must be exactly the string sent as `messageId` to `/inbound`: Qlar only accepts
    for an email the URLs it recorded under that id. Raises Revoked/Deleted when Qlar says so.
    """
    entries: list[dict[str, Any]] = []
    stop = False
    for position, item in enumerate(items):
        url = ""
        if (
            not stop
            and message_id
            and position < MAX_UPLOADED_ATTACHMENTS
            and item.data is not None
            and _worth_uploading(item)
        ):
            url, stop = _upload(client, message_id, sender, item, item.data, position)
        entries.append(
            {
                "fileName": item.file_name,
                "contentType": item.content_type,
                "sizeBytes": item.size_bytes,
                "url": url,
            }
        )
    return entries


def _walk(part: Message) -> Iterator[Message]:
    """Every part of a message, but never inside an attached `message/rfc822`."""
    yield part
    try:
        if not part.is_multipart() or part.get_content_type() == "message/rfc822":
            return
        children = part.get_payload()
    except Exception:  # noqa: BLE001 - a broken container has no readable children
        return
    if isinstance(children, list):
        for child in children:
            yield from _walk(child)


def _collect_part(part: Message, file_name: str, position: int) -> Attachment:
    content_type = part.get_content_type()
    estimate = _estimated_size(part)
    if (
        position >= MAX_UPLOADED_ATTACHMENTS
        or not _supported(file_name, content_type)
        or estimate > MAX_ATTACHMENT_BYTES
    ):
        return Attachment(file_name, content_type, None, estimate)

    data = _decode(part)
    if len(data) > MAX_ATTACHMENT_BYTES:
        return Attachment(file_name, content_type, None, len(data))
    return Attachment(file_name, content_type, data)


def _upload(
    client: _Uploader, message_id: str, sender: str, item: Attachment, data: bytes, position: int
) -> tuple[str, bool]:
    """Uploads one file; returns its URL ("" when not stored) and whether to stop uploading."""
    path = (
        f"{ATTACHMENTS_PATH}?messageId={_encode(message_id)}"
        f"&fileName={_encode(item.file_name)}&from={_encode(sender)}"
    )
    try:
        _, body = client.post_bytes(path, data, item.content_type, timeout=UPLOAD_TIMEOUT_SECONDS)
    except QlarRejected as rejection:
        if (stop := stop_reason(rejection)) is not None:
            raise stop from rejection
        logger.warning("attachment %d was not uploaded: HTTP %d", position + 1, rejection.status)
        pending = rejection.status == 403 and "pending" in str(rejection.body.get("reason", "")).lower()
        return "", pending
    except QlarUnreachable as error:
        logger.warning(
            "attachment %d was not uploaded: Qlar unreachable (%s)", position + 1, type(error).__name__
        )
        return "", True
    except httpx.InvalidURL:
        logger.warning("attachment %d was not uploaded: the request address was invalid", position + 1)
        return "", False

    url = body.get("url")
    if body.get("enabled") is False or not isinstance(url, str) or not url:
        logger.info("attachment %d was refused by Qlar: %s", position + 1, body.get("reason") or "no url")
        return "", False
    return url, False


def _worth_uploading(item: Attachment) -> bool:
    """A pre-filter that is never stricter than Qlar: it only saves sending bytes Qlar will refuse."""
    return item.size_bytes <= MAX_ATTACHMENT_BYTES and _supported(item.file_name, item.content_type)


def _supported(file_name: str, content_type: str) -> bool:
    if content_type.lower() in _IMAGE_TYPES:
        return True
    return file_name.lower().endswith(_SUPPORTED_EXTENSIONS)


def _encode(value: str) -> str:
    """Percent-encodes everything but RFC 3986 unreserved characters (letters, digits, `-._~`)."""
    return quote(value, safe="")


def _clean_name(name: str | None) -> str:
    """A safe file name: no directory part, no control characters, at most 255 characters.

    A name that is empty or only dots becomes "" (the caller substitutes a fallback). A longer
    name is cut in its stem so the extension survives.
    """
    if not name:
        return ""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base.strip("."):
        return ""
    if len(base) > MAX_NAME_CHARS:
        stem, dot, extension = base.rpartition(".")
        if not dot or len(extension) > 20:
            return base[:MAX_NAME_CHARS]
        return stem[: MAX_NAME_CHARS - len(extension) - 1] + "." + extension
    return base


def _estimated_size(part: Message) -> int:
    """Size of a part's content without decoding it (base64 is scaled by 3/4)."""
    payload = part.get_payload()
    if not isinstance(payload, str | bytes):
        return 0
    encoding = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
    return len(payload) * 3 // 4 if encoding == "base64" else len(payload)


def _decode(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise ValueError("part has no decodable payload")
    return payload
