"""Attachment safety: field validation, degrading to a plain reply, revocation, cid stripping."""

from __future__ import annotations

import hashlib
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from qlar_email_gateway.client import QlarRejected
from qlar_email_gateway.job_attachments import Fetched, fetch_job_attachments
from qlar_email_gateway.sender import build_reply

JOB_ID = "job_0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
JPG = b"\xff\xd8\xff" + b"jpeg" * 20
CID = "0123456789abcdef0123456789abcdef@qlar"
CID2 = "fedcba9876543210fedcba9876543210@qlar"


def entry(data: bytes = PNG, **overrides) -> dict:
    meta = {
        "fileName": "chart.png",
        "contentType": "image/png",
        "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "contentId": CID,
    }
    meta.update(overrides)
    return meta


def job_of(*entries: dict, **fields) -> dict:
    job = {
        "jobId": JOB_ID,
        "type": "send_email",
        "protocol": 2,
        "to": ["budi@customer.test"],
        "subject": "Re: Harga",
        "inReplyTo": "<m1@customer.test>",
        "references": ["<m1@customer.test>"],
        "textBody": "Halo",
        "htmlBody": f'<p>Hi</p><img src="cid:{CID}" alt="chart">',
        "agentId": "agt_1",
        "attachments": list(entries),
    }
    job.update(fields)
    return job


class FakeClient:
    def __init__(self, blobs):
        self.blobs = blobs
        self.paths: list[str] = []
        self.max_bytes: list[int | None] = []

    def get_bytes(self, path, *, max_bytes=None, timeout=30.0):
        self.paths.append(path)
        self.max_bytes.append(max_bytes)
        outcome = self.blobs[int(path.rsplit("/", 1)[1])]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def parse(msg: EmailMessage):
    return message_from_bytes(msg.as_bytes(), policy=policy.default)


# -- declared size bounds the download ------------------------------------------------------------


def test_declared_size_bounds_the_download():
    client = FakeClient({0: PNG})

    fetch_job_attachments(client, job_of(entry()))

    assert client.max_bytes == [len(PNG)]


def test_download_cap_never_exceeds_8mb():
    big = 8 * 1024 * 1024
    client = FakeClient({0: b"x" * big})

    fetch_job_attachments(client, job_of(entry(b"x" * big)))

    assert client.max_bytes == [big]


# -- job id ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["../x", "job_1", "job_" + "G" * 32, "job_" + "a" * 32 + "/x", ""])
def test_unexpected_job_id_skips_attachments_without_a_request(job_id):
    client = FakeClient({0: PNG})

    assert fetch_job_attachments(client, job_of(entry(), jobId=job_id)) is None
    assert client.paths == []


# -- entry validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"fileName": "a\r\nBcc: x@y.test.png"},
        {"fileName": 'a"b.png'},
        {"fileName": "a/b.png"},
        {"fileName": "a\\b.png"},
        {"fileName": "a;b.png"},
        {"fileName": "a<b>.png"},
        {"fileName": "a\tb.png"},
        {"fileName": ""},
        {"fileName": "x" * 101},
        {"contentType": "imagepng"},
        {"contentType": "image/png\r\nX: y"},
        {"contentType": "IMAGE/PNG"},
        {"contentId": f"<{CID}>"},
        {"contentId": "abc def@qlar"},
        {"contentId": CID + "\r\n"},
    ],
)
def test_invalid_entry_invalidates_the_whole_list(bad):
    client = FakeClient({0: PNG, 1: PNG})

    assert fetch_job_attachments(client, job_of(entry(), entry(**bad))) is None
    assert client.paths == []


def test_valid_entries_with_null_or_empty_content_id_are_accepted():
    client = FakeClient({0: PNG, 1: PNG})

    fetched = fetch_job_attachments(client, job_of(entry(contentId=None), entry(contentId="")))

    assert [f.content_id for f in fetched] == [None, None]


# -- poll: bad fields skip attachments but the reply is sent --------------------------------------


def _capture(harness):
    calls: list[dict] = []

    def sender(mail, job, **kwargs):
        calls.append(kwargs)
        return "<qlar-abc@corp.test>"

    harness.loop._send = sender
    return calls


@pytest.mark.parametrize(
    "bad",
    [{"fileName": "a\r\nBcc: x@y.test.png"}, {"contentType": "imagepng"}, {"contentId": f"<{CID}>"}],
    ids=["crlf-in-file-name", "content-type-without-slash", "content-id-with-angle-brackets"],
)
def test_bad_attachment_field_skips_attachments_but_sends_reply(harness, bad):
    calls = _capture(harness)
    harness.loop.client.get_bytes = FakeClient({0: PNG}).get_bytes
    job = harness.job(protocol=2, jobId=JOB_ID, attachments=[entry(**bad)])

    harness.loop._run_job(job)

    assert calls[0]["attachments"] is None
    assert harness.results()[0]["status"] == "ok"
    assert [e["status"] for e in harness.audit()] == ["attachments_skipped", "sent"]


def test_crlf_in_file_name_skips_attachments_but_sends_reply(harness):
    test_bad_attachment_field_skips_attachments_but_sends_reply(harness, {"fileName": "a\r\nb.png"})


def test_content_type_without_slash_skips_attachments_but_sends_reply(harness):
    test_bad_attachment_field_skips_attachments_but_sends_reply(harness, {"contentType": "png"})


def test_content_id_with_angle_brackets_skips_attachments(harness):
    test_bad_attachment_field_skips_attachments_but_sends_reply(harness, {"contentId": f"<{CID}>"})


# -- revocation during the fetch ------------------------------------------------------------------


@pytest.mark.parametrize(("reason", "expected"), [("revoked", "Revoked"), ("unknown_gateway", "Deleted")])
def test_revocation_during_fetch_stops_the_loop_without_sending(harness, reason, expected):
    from qlar_email_gateway.client import Deleted, Revoked

    calls = _capture(harness)
    harness.loop.client.get_bytes = FakeClient({0: QlarRejected(403, "no", {"reason": reason})}).get_bytes

    harness.loop._run_job(harness.job(protocol=2, jobId=JOB_ID, attachments=[entry()]))

    assert calls == []
    assert harness.results() == []
    assert isinstance(harness.loop.stopped_by, Deleted if expected == "Deleted" else Revoked)
    assert type(harness.loop.stopped_by).__name__ == expected
    assert harness.loop._stopping.is_set()
    assert harness.audit() == []


# -- build_reply degrades -------------------------------------------------------------------------


def test_unexpected_value_error_while_building_degrades_to_plain_reply(mail, monkeypatch):
    def boom(self, *args, **kwargs):
        raise ValueError("header injection")

    monkeypatch.setattr(EmailMessage, "add_related", boom)
    job = job_of(entry())

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", CID, PNG)])

    mime = parse(msg)
    assert "cid:" not in msg.as_string()
    assert "<img" not in mime.get_body(("html",)).get_content()
    assert mime.get_body(("plain",)).get_content().strip() == "Halo"
    assert list(mime.iter_attachments()) == []


def test_unexpected_error_adding_a_plain_attachment_degrades_too(mail, monkeypatch):
    def boom(self, *args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(EmailMessage, "add_attachment", boom)
    job = job_of(entry(contentId=None), htmlBody="<p>Hi</p>")

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", None, PNG)])

    assert parse(msg).get_body(("plain",)).get_content().strip() == "Halo"


# -- cid stripping --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        "<img src='cid:X' alt='a'>",
        '<img src = "cid:X">',
        '<img alt="a>b" src="cid:X">',
        "<img src=cid:X>",
        '<IMG SRC="CID:X" ALT="x">',
        '<img\n  style="a:b"\n  src="cid:X"\n/>',
        '<img data-x="y" alt=\'a>b\' src=\'cid:X\' width="3">',
    ],
)
def test_cid_img_tags_are_stripped_in_every_spelling(mail, tag):
    html = f"<p>before</p>{tag}<p>after</p>"
    job = job_of(entry(), htmlBody=html)

    msg = build_reply(mail, job, "<qlar-1@corp.test>", None)

    body = parse(msg).get_body(("html",)).get_content()
    assert "cid:" not in body.lower()
    assert "<p>before</p>" in body and "<p>after</p>" in body
    assert "a>b" not in body or "<img" not in body.lower()


def test_remote_images_are_kept_when_cid_images_are_stripped(mail):
    html = f'<img src="https://x.test/a.png"><img src="cid:{CID}">'

    msg = build_reply(mail, job_of(entry(), htmlBody=html), "<qlar-1@corp.test>", None)

    body = parse(msg).get_body(("html",)).get_content()
    assert "https://x.test/a.png" in body and "cid:" not in body


def test_leftover_cid_reference_is_removed_by_the_fallback(mail):
    html = f'<p>x</p><img src="cid:{CID}" <broken> <span style="background:url(cid:{CID})">y</span>'

    msg = build_reply(mail, job_of(entry(), htmlBody=html), "<qlar-1@corp.test>", None)

    assert "cid:" not in parse(msg).get_body(("html",)).get_content().lower()


# -- multipart shapes -----------------------------------------------------------------------------


def _types(msg) -> list[str]:
    return [part.get_content_type() for part in parse(msg).walk()]


def test_mixed_inline_and_regular_attachments_make_the_expected_tree(mail):
    job = job_of(entry(), entry(JPG, fileName="b.jpg", contentType="image/jpeg", contentId=None))
    fetched = [
        Fetched("chart.png", "image/png", CID, PNG),
        Fetched("b.jpg", "image/jpeg", None, JPG),
    ]

    msg = build_reply(mail, job, "<qlar-1@corp.test>", fetched)

    assert _types(msg) == [
        "multipart/mixed",
        "multipart/alternative",
        "text/plain",
        "multipart/related",
        "text/html",
        "image/png",
        "image/jpeg",
    ]


def test_two_inline_pictures_are_both_related_to_the_html(mail):
    html = f'<img src="cid:{CID}"><img src="cid:{CID2}">'
    second = entry(JPG, fileName="b.jpg", contentType="image/jpeg", contentId=CID2)
    job = job_of(entry(), second, htmlBody=html)
    fetched = [Fetched("chart.png", "image/png", CID, PNG), Fetched("b.jpg", "image/jpeg", CID2, JPG)]

    msg = build_reply(mail, job, "<qlar-1@corp.test>", fetched)

    mime = parse(msg)
    related = next(p for p in mime.walk() if p.get_content_type() == "multipart/related")
    assert [p.get_content_type() for p in related.iter_parts()] == ["text/html", "image/png", "image/jpeg"]
    assert [p["Content-ID"] for p in list(related.iter_parts())[1:]] == [f"<{CID}>", f"<{CID2}>"]


def test_empty_html_falls_back_to_ordinary_attachments(mail):
    job = job_of(entry(), htmlBody="")

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", CID, PNG)])

    mime = parse(msg)
    assert mime.get_content_type() == "multipart/mixed"
    (attachment,) = list(mime.iter_attachments())
    assert attachment.get_filename() == "chart.png"
    assert attachment["Content-ID"] is None
