"""Hardening of attachment collection and upload: nesting, memory, per-part failures, stop rules."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from test_attachments import FakeUploader, email_with, item

from qlar_email_gateway.attachments import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    collect_attachments,
    upload_all,
)
from qlar_email_gateway.client import QlarNotAnEndpoint, QlarRejected, QlarUnreachable

PDF = b"%PDF-1.4 fake"


def _parse(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _raw(*parts: bytes) -> bytes:
    return (
        b"From: a@b.test\r\nTo: ask@corp.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n" + b"".join(parts) + b"--B--\r\n"
    )


def _pdf_part(name: str, payload: bytes = b"JVBERg==") -> bytes:
    return (
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="' + name.encode() + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n" + payload + b"\r\n"
    )


def _attached_message() -> bytes:
    return (
        b"--B\r\nContent-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="fwd.eml"\r\n\r\n'
        b"From: x@y.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="I"\r\n\r\n'
        b"--I\r\nContent-Type: text/plain\r\n\r\ninner\r\n"
        b"--I\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="inner.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--I--\r\n"
    )


def _record_decodes(msg) -> list[str]:
    """Patches every part so a decoding get_payload call is recorded by file name."""
    decoded: list[str] = []
    for part in msg.walk():
        original = part.get_payload
        name = part.get_filename() or ""

        def wrapper(*args, _original=original, _name=name, **kwargs):
            if kwargs.get("decode") or (len(args) > 1 and args[1]):
                decoded.append(_name)
            return _original(*args, **kwargs)

        part.get_payload = wrapper
    return decoded


# -- 1. nested message/rfc822 ---------------------------------------------------------------------


def test_nested_rfc822_is_not_collected_and_children_not_listed():
    msg = _parse(_raw(_attached_message(), _pdf_part("real.pdf")))

    assert [a.file_name for a in collect_attachments(msg)] == ["real.pdf"]


def test_real_attachment_after_forwarded_message_keeps_its_slot():
    msg = _parse(_raw(_attached_message() * 3, *[_pdf_part(f"r{i}.pdf") for i in range(5)]))

    uploader = FakeUploader()
    entries = upload_all(uploader, "<m>", "a@b.test", collect_attachments(msg))

    assert [e["fileName"] for e in entries] == [f"r{i}.pdf" for i in range(5)]
    assert len(uploader.calls) == 5


# -- 2. lazy decoding -----------------------------------------------------------------------------


def test_parts_beyond_the_fifth_are_not_decoded():
    msg = _parse(_raw(*[_pdf_part(f"f{i}.pdf") for i in range(7)]))
    decoded = _record_decodes(msg)

    found = collect_attachments(msg)

    assert [a.file_name for a in found] == [f"f{i}.pdf" for i in range(7)]
    assert decoded == [f"f{i}.pdf" for i in range(5)]
    assert all(a.size_bytes > 0 for a in found)


def test_unsupported_part_is_not_decoded():
    msg = _parse(
        _raw(
            b"--B\r\nContent-Type: application/x-msdownload\r\n"
            b'Content-Disposition: attachment; filename="a.exe"\r\n'
            b"Content-Transfer-Encoding: base64\r\n\r\nAAAA\r\n"
        )
    )
    decoded = _record_decodes(msg)

    found = collect_attachments(msg)

    assert [a.file_name for a in found] == ["a.exe"]
    assert decoded == []


def test_oversized_part_is_not_decoded():
    huge = b"A" * (MAX_ATTACHMENT_BYTES * 4 // 3 + 4000)
    msg = _parse(_raw(_pdf_part("big.pdf", huge)))
    decoded = _record_decodes(msg)

    found = collect_attachments(msg)
    uploader = FakeUploader()
    entries = upload_all(uploader, "<m>", "a@b.test", found)

    assert decoded == []
    assert uploader.calls == []
    assert entries[0]["url"] == ""
    assert entries[0]["sizeBytes"] > MAX_ATTACHMENT_BYTES


def test_decoded_size_is_exact_for_uploaded_parts():
    msg = _parse(_raw(_pdf_part("a.pdf", base64.b64encode(PDF))))

    entries = upload_all(FakeUploader(), "<m>", "a@b.test", collect_attachments(msg))

    assert entries[0]["sizeBytes"] == len(PDF)


# -- 3. per-part failure --------------------------------------------------------------------------


def test_part_that_fails_to_decode_costs_one_attachment_not_the_email():
    msg = _parse(_raw(_pdf_part("bad.pdf"), _pdf_part("good.pdf")))
    bad = next(p for p in msg.walk() if p.get_filename() == "bad.pdf")
    original = bad.get_payload

    def boom(*args, **kwargs):
        if kwargs.get("decode"):
            raise ValueError("broken")
        return original(*args, **kwargs)

    bad.get_payload = boom

    uploader = FakeUploader()
    entries = upload_all(uploader, "<m>", "a@b.test", collect_attachments(msg))

    assert [e["fileName"] for e in entries] == ["bad.pdf", "good.pdf"]
    assert [e["url"] != "" for e in entries] == [False, True]


# -- 4. stop rules --------------------------------------------------------------------------------


def test_unreachable_stops_further_uploads():
    uploader = FakeUploader(QlarUnreachable("down"))

    entries = upload_all(uploader, "<m>", "a@b.test", [item(f"f{i}.pdf") for i in range(4)])

    assert len(uploader.calls) == 1
    assert [e["url"] for e in entries] == [""] * 4


def test_pending_approval_stops_further_uploads():
    uploader = FakeUploader(QlarRejected(403, "no", {"reason": "pending_approval"}))

    entries = upload_all(uploader, "<m>", "a@b.test", [item(f"f{i}.pdf") for i in range(3)])

    assert len(uploader.calls) == 1
    assert [e["url"] for e in entries] == [""] * 3


def test_invalid_url_error_is_treated_as_not_uploaded():
    uploader = FakeUploader(httpx.InvalidURL("bad"))

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])

    assert entries[0]["url"] == ""


def test_not_an_endpoint_is_not_uploaded():
    uploader = FakeUploader(QlarNotAnEndpoint(404, "https://x/attachments", "text/html"))

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])

    assert entries[0]["url"] == ""


# -- 5. names and types ---------------------------------------------------------------------------


def test_very_long_file_name_is_capped_keeping_extension():
    msg = email_with(("application/pdf", "attachment", PDF, {"filename": "a" * 400 + ".pdf"}))

    name = collect_attachments(msg)[0].file_name

    assert len(name) == 255
    assert name.endswith(".pdf")


@pytest.mark.parametrize("name", [".", "..", "../.."])
def test_dot_dot_name_is_normalised(name):
    msg = email_with(("application/pdf", "attachment", PDF, {"filename": name}))

    assert collect_attachments(msg)[0].file_name == "attachment"


def test_malformed_content_type_is_listed_as_text_plain():
    msg = _parse(
        _raw(
            b"--B\r\nContent-Type: pdf\r\n"
            b'Content-Disposition: attachment; filename="notes.txt"\r\n\r\nhello\r\n'
        )
    )

    found = collect_attachments(msg)

    assert [(a.file_name, a.content_type) for a in found] == [("notes.txt", "text/plain")]


def test_inline_part_without_filename_and_without_cid_is_ignored():
    msg = _parse(_raw(b"--B\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n\r\nxx\r\n"))

    assert collect_attachments(msg) == []


def test_attachment_dataclass_still_takes_plain_bytes():
    assert Attachment("a.pdf", "application/pdf", b"abc").size_bytes == 3
