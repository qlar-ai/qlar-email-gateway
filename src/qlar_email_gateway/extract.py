"""What of an email is forwarded to Qlar: readable text and the /inbound payload (FR-23, FR-24).

Only what the agent needs to answer leaves the network: the sender, the recipients, the threading
headers and the text the person actually wrote. The quoted history underneath a reply is cut —
the agent already has the conversation, and re-sending every earlier message with every reply
would grow each email until it no longer fit.
"""

from __future__ import annotations

import re
from datetime import UTC
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

_QUOTE_LINE = re.compile(r"^\s*>")
_CUT_MARKERS = (
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^Pada .+ menulis:\s*$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-- $"),
)
_OUTLOOK_FROM = re.compile(r"^\s*(From|Dari)\s*:", re.IGNORECASE)
_OUTLOOK_SENT = re.compile(r"^\s*(Sent|Dikirim|Date|Tanggal)\s*:", re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")
_AUTHENTICATION_RESULTS_LIMIT = 2000
_QUOTE_PREFIX = re.compile(r"^(?:\s*>)+ ?")
_FORWARD_SUBJECT = re.compile(r"^\s*(fwd?|tr|ts|teruskan)\s*:", re.IGNORECASE)
_FORWARD_BANNER = re.compile(
    r"^\s*(?:-{2,}\s*(?:Forwarded message|Pesan (?:terusan|yang diteruskan))\s*-{2,}"
    r"|Begin forwarded message:|Awal pesan terusan:)\s*$",
    re.IGNORECASE,
)


def extract_text(msg: EmailMessage, limit: int) -> tuple[str, bool]:
    """The readable text of an email, quoted history removed, cut to `limit` characters.

    `text/plain` is preferred; an HTML-only email is converted to text with its `<style>` and
    `<script>` removed. Returns the text and whether it was truncated.
    """
    plain = _part_text(msg, "text/plain")
    if plain is not None:
        text = plain
    else:
        html = _part_text(msg, "text/html")
        text = _html_to_text(html) if html is not None else ""

    text = _normalize(text)
    text = _keep_forwarded(text) if is_forward(msg, text) else strip_quoted_reply(text)
    if len(text) > limit:
        return text[:limit], True
    return text, False


def strip_quoted_reply(text: str) -> str:
    """Removes quoted lines and everything from a reply header or signature separator on.

    If that would leave nothing, the original text is kept: forwarding a quote is better than
    forwarding silence when the sender did write something.
    """
    lines = text.split("\n")
    kept: list[str] = []

    for index, line in enumerate(lines):
        if any(marker.match(line) for marker in _CUT_MARKERS):
            break
        if _OUTLOOK_FROM.match(line) and any(
            _OUTLOOK_SENT.match(after) for after in lines[index + 1 : index + 5]
        ):
            break
        if _QUOTE_LINE.match(line):
            continue
        kept.append(line)

    stripped = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    return stripped if stripped else text.strip()


def is_forward(msg: Message, text: str) -> bool:
    """Whether the sender forwarded someone else's email instead of replying to a conversation.

    A forward is the content: the person forwarding it wants the agent to answer what is in the
    block, so it must not be cut like quoted history. Detected by a `Fwd:`/`Fw:` subject, or by a
    forward banner that comes before any reply header (a banner buried under an `On … wrote:`
    line is just an old forward inside a reply thread).
    """
    if _FORWARD_SUBJECT.match(str(msg.get("Subject") or "")):
        return True

    for line in text.split("\n"):
        if any(marker.match(line) for marker in _CUT_MARKERS):
            return False
        if _FORWARD_BANNER.match(line):
            return True
    return False


def _keep_forwarded(text: str) -> str:
    """The text of a forward with the quote marks removed and nothing cut."""
    lines = [_QUOTE_PREFIX.sub("", line) for line in text.split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def build_inbound_payload(
    msg: EmailMessage, uid: int, limit: int, attachments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The `POST /inbound` body for one email, exactly the keys of PRD FR-24 / PROTOCOL.md §8.

    `attachments` are the `{fileName, contentType, sizeBytes, url}` entries from `upload_all`.
    """
    text, truncated = extract_text(msg, limit)
    from_name, from_address = parseaddr(str(msg.get("From") or ""))

    return {
        "messageId": str(msg.get("Message-ID") or "").strip(),
        "uid": uid,
        "from": {"address": from_address.strip().lower(), "name": from_name.strip()},
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "replyTo": _single_address(msg.get("Reply-To")),
        "subject": _single_line(msg.get("Subject")),
        "date": _iso_date(msg.get("Date")),
        "inReplyTo": _single_line(msg.get("In-Reply-To")) or None,
        "references": _single_line(msg.get("References")).split(),
        "text": text,
        "textTruncated": truncated,
        "attachments": list(attachments or []),
        "headers": {
            "autoSubmitted": _header_or_none(msg, "Auto-Submitted"),
            "precedence": _header_or_none(msg, "Precedence"),
            "listId": _header_or_none(msg, "List-Id"),
            "authenticationResults": _authentication_results(msg),
        },
    }


def _part_text(msg: Message, content_type: str) -> str | None:
    """The first non-attachment part of the given type, decoded with its own charset."""
    for part in msg.walk():
        if part.get_content_type() != content_type or part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:  # an unknown charset name
            return payload.decode("utf-8", errors="replace")
    return None


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["style", "script", "head"]):
        element.decompose()
    return soup.get_text("\n")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() if line != "-- " else line for line in text.split("\n")]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()


def _addresses(msg: EmailMessage, header: str) -> list[dict[str, str]]:
    values = [str(value) for value in msg.get_all(header, [])]
    return [
        {"address": address.strip().lower(), "name": name.strip()}
        for name, address in getaddresses(values)
        if address.strip()
    ]


def _single_address(value: object) -> str | None:
    """A bare lowercased address when the header holds exactly one, else None (Qlar then answers From)."""
    if not value:
        return None
    found = [address for _, address in getaddresses([str(value)]) if address.strip()]
    return found[0].strip().lower() if len(found) == 1 else None


def _single_line(value: object) -> str:
    """A header value with folding and control characters collapsed to single spaces."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()


def _header_or_none(msg: EmailMessage, name: str) -> str | None:
    value = msg.get(name)
    return _single_line(value) if value is not None else None


def _authentication_results(msg: EmailMessage) -> str | None:
    """The topmost `Authentication-Results` header, which the mailbox's own server added last.

    It carries that server's SPF, DKIM and DMARC verdicts, so Qlar can tell a forged `From` from
    a real one. Only the first is read: a sender can put their own copies further down, but not
    above the one the receiving server prepends. Capped, because it is forwarded on every email.
    """
    value = _header_or_none(msg, "Authentication-Results")
    return value[:_AUTHENTICATION_RESULTS_LIMIT] if value else None


def _iso_date(value: object) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
