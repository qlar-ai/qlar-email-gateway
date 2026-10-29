"""Inbound attachments: which parts are collected, which are uploaded, and how the upload is signed."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from qlar_email_gateway import attachments as attachments_module
from qlar_email_gateway import client as client_module
from qlar_email_gateway.attachments import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    collect_attachments,
    upload_all,
)
from qlar_email_gateway.client import QlarClient, QlarRejected, QlarUnreachable, Revoked
from qlar_email_gateway.crypto import canonical_request, generate_private_key, verify

PDF = b"%PDF-1.4 fake"
JPEG = b"\xff\xd8\xff fake"


def email_with(*parts: tuple[str, str, bytes, dict]) -> EmailMessage:
    """parts: ("type/subtype", disposition, data, extra) with extra = {filename, cid}."""
    msg = EmailMessage()
    msg["From"] = "a@b.test"
    msg["To"] = "ask@corp.test"
    msg.set_content("hello")
    for content_type, disposition, data, extra in parts:
        main, sub = content_type.split("/")
        kwargs = {"disposition": disposition}
        if extra.get("filename") is not None:
            kwargs["filename"] = extra["filename"]
        if extra.get("cid"):
            kwargs["cid"] = extra["cid"]
        msg.add_attachment(data, maintype=main, subtype=sub, **kwargs)
    return msg


class FakeUploader:
    def __init__(self, outcome=None):
        self.calls: list[dict] = []
        self.outcome = outcome

    def post_bytes(self, path, body, content_type, *, timeout=60.0):
        self.calls.append({"path": path, "body": body, "content_type": content_type})
        if callable(self.outcome):
            return self.outcome(len(self.calls), path)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return 200, {"url": f"https://cdn.test/{len(self.calls)}"}


def item(name: str, content_type: str = "application/pdf", size: int = 10) -> Attachment:
    return Attachment(name, content_type, b"x" * size)


# -- collect --------------------------------------------------------------------------------------


def test_collects_pdf_and_jpeg_attachments():
    msg = email_with(
        ("application/pdf", "attachment", PDF, {"filename": "offer.pdf"}),
        ("image/jpeg", "attachment", JPEG, {"filename": "photo.jpg"}),
    )

    found = collect_attachments(msg)

    assert [(a.file_name, a.content_type, a.data) for a in found] == [
        ("offer.pdf", "application/pdf", PDF),
        ("photo.jpg", "image/jpeg", JPEG),
    ]


def test_inline_cid_images_are_ignored():
    msg = email_with(
        ("image/png", "inline", b"logo", {"filename": "logo.png", "cid": "<logo@x>"}),
        ("application/pdf", "attachment", PDF, {"filename": "offer.pdf"}),
    )

    assert [a.file_name for a in collect_attachments(msg)] == ["offer.pdf"]


def test_inline_with_filename_and_no_content_id_is_an_attachment():
    msg = email_with(("image/png", "inline", b"pic", {"filename": "pic.png"}))

    assert [a.file_name for a in collect_attachments(msg)] == ["pic.png"]


def test_inline_without_filename_is_ignored():
    msg = email_with(("image/png", "inline", b"pic", {}))

    assert collect_attachments(msg) == []


def test_directory_components_are_stripped_from_file_names():
    msg = email_with(
        ("application/pdf", "attachment", PDF, {"filename": "C:\\Users\\bob\\offer.pdf"}),
        ("application/pdf", "attachment", PDF, {"filename": "../../etc/notes.pdf"}),
    )

    assert [a.file_name for a in collect_attachments(msg)] == ["offer.pdf", "notes.pdf"]


def test_encoded_non_ascii_file_names_are_decoded():
    raw = (
        b"From: a@b.test\r\nTo: ask@corp.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=UTF-8''laporan%20%C3%A9t%C3%A9.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?caf=C3=A9.pdf?="\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--B--\r\n"
    )
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    assert [a.file_name for a in collect_attachments(msg)] == ["laporan \u00e9t\u00e9.pdf", "caf\u00e9.pdf"]


# -- upload_all -----------------------------------------------------------------------------------


def test_upload_sends_bytes_with_content_type_and_encoded_query():
    uploader = FakeUploader()
    entries = upload_all(uploader, "<m1@x>", "bob@c.test", [Attachment("offer.pdf", "application/pdf", PDF)])

    call = uploader.calls[0]
    assert call["body"] == PDF
    assert call["content_type"] == "application/pdf"
    assert call["path"] == "/attachments?messageId=%3Cm1%40x%3E&fileName=offer.pdf&from=bob%40c.test"
    assert entries == [
        {
            "fileName": "offer.pdf",
            "contentType": "application/pdf",
            "sizeBytes": len(PDF),
            "url": "https://cdn.test/1",
        }
    ]


def test_query_values_use_only_the_unreserved_set():
    uploader = FakeUploader()
    name = "laporan \u00e9t\u00e9 & q#1 +x.pdf"
    upload_all(uploader, "<a b&c#d@x>", "bob@c.test", [Attachment(name, "application/pdf", PDF)])

    query = uploader.calls[0]["path"].split("?", 1)[1]
    assert all((ch.isalnum() and ch.isascii()) or ch in "-._~%&=" for ch in query)
    assert "fileName=laporan%20%C3%A9t%C3%A9%20%26%20q%231%20%2Bx.pdf" in query
    assert "messageId=%3Ca%20b%26c%23d%40x%3E" in query
    decoded = dict(parse_qsl(query, keep_blank_values=True))
    assert decoded["fileName"] == name
    assert decoded["messageId"] == "<a b&c#d@x>"


def test_unsupported_type_listed_without_upload():
    uploader = FakeUploader()
    entries = upload_all(uploader, "<m>", "a@b.test", [item("setup.exe", "application/x-msdownload")])

    assert uploader.calls == []
    assert entries == [
        {"fileName": "setup.exe", "contentType": "application/x-msdownload", "sizeBytes": 10, "url": ""}
    ]


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpg"),
        ("a.png", "image/png"),
        ("a.webp", "image/webp"),
        ("a.pdf", "application/pdf"),
        ("a.pdf", "application/octet-stream"),
        ("notes.txt", "text/plain"),
        ("notes.md", "text/markdown"),
        ("NOTES.MD", "application/octet-stream"),
        ("scan", "image/png"),
    ],
)
def test_supported_types_are_uploaded(name, content_type):
    uploader = FakeUploader()
    upload_all(uploader, "<m>", "a@b.test", [item(name, content_type)])

    assert len(uploader.calls) == 1


def test_over_20mb_listed_without_upload():
    uploader = FakeUploader()
    big = Attachment("big.pdf", "application/pdf", b"x" * (MAX_ATTACHMENT_BYTES + 1))
    exact = Attachment("exact.pdf", "application/pdf", b"x" * MAX_ATTACHMENT_BYTES)

    entries = upload_all(uploader, "<m>", "a@b.test", [big, exact])

    assert len(uploader.calls) == 1
    assert uploader.calls[0]["body"] == exact.data
    assert entries[0]["url"] == ""
    assert entries[0]["sizeBytes"] == MAX_ATTACHMENT_BYTES + 1
    assert entries[1]["url"] != ""


def test_sixth_attachment_not_uploaded():
    uploader = FakeUploader()
    entries = upload_all(uploader, "<m>", "a@b.test", [item(f"f{i}.pdf") for i in range(6)])

    assert len(uploader.calls) == 5
    assert [e["url"] != "" for e in entries] == [True] * 5 + [False]
    assert entries[5]["fileName"] == "f5.pdf"


def test_first_five_by_position_even_when_earlier_ones_are_refused():
    uploader = FakeUploader()
    items = [
        item("a.exe", "application/x-msdownload"),
        item("b.pdf"),
        item("c.pdf"),
        item("d.pdf"),
        item("e.pdf"),
        item("f.pdf"),
        item("g.pdf"),
    ]

    entries = upload_all(uploader, "<m>", "a@b.test", items)

    assert [e["url"] != "" for e in entries] == [False, True, True, True, True, False, False]
    assert len(uploader.calls) == 4


def test_refusal_from_server_gives_empty_url():
    uploader = FakeUploader(lambda n, p: (200, {"enabled": False, "url": "", "reason": "too-large"}))

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])

    assert entries[0]["url"] == ""


@pytest.mark.parametrize("status", [403, 413, 401, 500])
def test_upload_failure_still_forwards_and_names_file(status):
    uploader = FakeUploader(QlarRejected(status, "no", {"reason": "pending_approval"}))

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf"), item("b.pdf")])

    assert [(e["fileName"], e["url"]) for e in entries] == [("a.pdf", ""), ("b.pdf", "")]


def test_unreachable_upload_is_not_uploaded():
    uploader = FakeUploader(QlarUnreachable("down"))

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])

    assert entries[0]["url"] == ""


@pytest.mark.parametrize("reason", ["revoked", "unknown_gateway"])
def test_revoked_during_upload_still_stops(reason):
    uploader = FakeUploader(QlarRejected(403, "no", {"reason": reason}))

    with pytest.raises(Revoked):
        upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])


def test_failed_upload_does_not_block_the_next_one():
    def outcome(n, path):
        if n == 1:
            raise QlarRejected(500, "x")
        return 200, {"url": "u2"}

    uploader = FakeUploader(outcome)

    entries = upload_all(uploader, "<m>", "a@b.test", [item("a.pdf"), item("b.pdf")])

    assert [e["url"] for e in entries] == ["", "u2"]


def test_blank_message_id_is_listed_without_upload():
    uploader = FakeUploader()

    entries = upload_all(uploader, "", "a@b.test", [item("a.pdf")])

    assert uploader.calls == []
    assert entries[0]["url"] == ""


def test_no_file_name_or_sender_reaches_the_log(caplog):
    uploader = FakeUploader(QlarRejected(500, "x"))
    with caplog.at_level("DEBUG"):
        upload_all(uploader, "<m>", "secret@c.test", [item("secret-plan.pdf")])

    assert "secret" not in caplog.text
    assert attachments_module.logger.name == "qlar_email_gateway.attachments"


# -- signed transport -----------------------------------------------------------------------------


def _mock_httpx(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )


def test_attachment_path_is_signed_with_query(monkeypatch):
    key = generate_private_key()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": "https://cdn.test/x"})

    _mock_httpx(monkeypatch, handler)
    qlar = QlarClient("https://q.test/api/email-gateway/", key, "egw_1")
    path = "/attachments?messageId=%3Cm1%40x%3E&fileName=a%20b.pdf&from=bob%40c.test"

    status, body = qlar.post_bytes(path, PDF, "application/pdf")

    request = seen[0]
    assert (status, body) == (200, {"url": "https://cdn.test/x"})
    assert str(request.url) == "https://q.test/api/email-gateway" + path
    assert request.content == PDF
    assert request.headers["content-type"] == "application/pdf"
    assert request.headers["x-qlar-protocol"] == "2"
    assert request.headers["x-qlar-gateway-id"] == "egw_1"
    canonical = canonical_request(
        "POST", path, request.headers["x-qlar-timestamp"], request.headers["x-qlar-nonce"], PDF
    )
    assert verify(key.public_key(), canonical, request.headers["x-qlar-signature"])
    assert urlsplit(str(request.url)).query == path.split("?", 1)[1]


def test_post_bytes_raises_rejected_on_413(monkeypatch):
    key = generate_private_key()
    _mock_httpx(monkeypatch, lambda request: httpx.Response(413, json={}))
    qlar = QlarClient("https://q.test/api", key, "egw_1")

    with pytest.raises(QlarRejected) as raised:
        qlar.post_bytes("/attachments?a=b", b"x", "application/pdf")

    assert raised.value.status == 413
