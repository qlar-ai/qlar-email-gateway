"""Final hardening: hostile content types, text safety of the cid fallback, message/* parts."""

from __future__ import annotations

import httpx
import pytest
from test_attachments import FakeUploader, item
from test_attachments_hardening import _parse, _raw
from test_job_attachments_hardening import CID, build_reply, entry, job_of, parse

from qlar_email_gateway.attachments import collect_attachments, upload_all
from qlar_email_gateway.client import QlarRejected, Revoked

# -- final wave: hostile content types, text safety, message/* parts ------------------------------


def _ct_part(content_type: bytes, name: bytes = b"x.png") -> bytes:
    return (
        b"--B\r\n" + content_type + b"\r\n"
        b'Content-Disposition: attachment; filename="' + name + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\naGVsbG8=\r\n"
    )


def test_non_ascii_content_type_is_replaced_not_sent():
    msg = _parse(_raw(_ct_part(b"Content-Type: image/png\xc3\xa9")))
    uploader = FakeUploader()

    found = collect_attachments(msg)
    entries = upload_all(uploader, "<m>", "a@b.test", found)

    assert found[0].content_type == "application/octet-stream"
    assert uploader.calls[0]["content_type"] == "application/octet-stream"
    assert entries[0]["contentType"] == "application/octet-stream"


def test_non_ascii_content_type_does_not_escape_the_real_client(monkeypatch):
    from qlar_email_gateway import client as client_module
    from qlar_email_gateway.client import QlarClient
    from qlar_email_gateway.crypto import generate_private_key

    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"url": "https://c/x"})),
            **kw,
        ),
    )
    qlar = QlarClient("https://q.test/api", generate_private_key(), "egw_1")
    msg = _parse(_raw(_ct_part(b"Content-Type: image/png\xc3\xa9")))

    entries = upload_all(qlar, "<m>", "a@b.test", collect_attachments(msg))

    assert entries[0]["url"] == "https://c/x"


@pytest.mark.parametrize("raw_type", [b"Content-Type: image/png\x01", b"Content-Type: image /png"])
def test_control_character_content_type_is_replaced(raw_type):
    msg = _parse(_raw(_ct_part(raw_type)))

    assert collect_attachments(msg)[0].content_type in ("application/octet-stream", "image/png")
    assert all(ch.isascii() and ch.isprintable() for ch in collect_attachments(msg)[0].content_type)


def test_content_type_token_is_lower_cased_and_kept_when_ascii():
    msg = _parse(_raw(_ct_part(b"Content-Type: Application/PDF", b"a.pdf")))

    assert collect_attachments(msg)[0].content_type == "application/pdf"


ERRORS = [UnicodeEncodeError("ascii", "é", 0, 1, "x"), ValueError("v"), RuntimeError("r")]


@pytest.mark.parametrize("error", ERRORS)
def test_unexpected_exception_while_uploading_costs_one_file_not_the_loop(error, caplog):
    def outcome(n, path):
        if n == 1:
            raise error
        return 200, {"url": "u2"}

    uploader = FakeUploader(outcome)

    with caplog.at_level("DEBUG"):
        entries = upload_all(uploader, "<m>", "secret@c.test", [item("a.pdf"), item("b.pdf")])

    assert [e["url"] for e in entries] == ["", "u2"]
    assert "secret" not in caplog.text and "a.pdf" not in caplog.text


@pytest.mark.parametrize("reason", ["revoked", "unknown_gateway"])
def test_revoked_still_propagates_through_the_final_except(reason):
    uploader = FakeUploader(QlarRejected(403, "no", {"reason": reason}))

    with pytest.raises(Revoked):
        upload_all(uploader, "<m>", "a@b.test", [item("a.pdf")])


def test_mailbox_survives_a_hostile_content_type(tmp_path):
    from test_mailbox import FakeImap, Harness

    raw = (
        b"From: a@b.test\r\nTo: ask@corp.test\r\nSubject: s\r\nMessage-ID: <m11@b.test>\r\n"
        b"Date: Wed, 07 Oct 2026 09:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        + _ct_part(b"Content-Type: image/png\xc3\xa9")
        + b"--B--\r\n"
    )
    harness = Harness(tmp_path, FakeImap({11: raw}))

    harness.run_until_poll_wait()

    assert harness.state.last_uid == 11
    assert harness.qlar.posts[0]["attachments"][0]["contentType"] == "application/octet-stream"


def test_acid_and_lucid_text_is_left_alone(mail):
    html = f'<p>Folic acid:5mg, lucid:yes</p><img src="cid:{CID}" <broken>'

    msg = build_reply(mail, job_of(entry(), htmlBody=html), "<qlar-1@corp.test>", None)

    body = parse(msg).get_body(("html",)).get_content()
    assert "Folic acid:5mg, lucid:yes" in body
    assert CID not in body and "<img" not in body.lower()


def test_message_parts_are_neither_collected_nor_descended_into():
    delivery = (
        b"--B\r\nContent-Type: message/delivery-status\r\n"
        b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
        b"Reporting-MTA: dns; x\r\n\r\n"
    )
    glob = (
        b"--B\r\nContent-Type: message/global\r\n"
        b'Content-Disposition: attachment; filename="g.eml"\r\n\r\n'
        b"From: x@y.test\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="I"\r\n\r\n'
        b"--I\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="inner.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--I--\r\n"
    )
    msg = _parse(_raw(delivery, glob, _ct_part(b"Content-Type: image/png")))

    assert [a.file_name for a in collect_attachments(msg)] == ["x.png"]
