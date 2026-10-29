"""Outbound attachments (protocol 2): signature line, signed fetch, hash checks, multipart reply."""

from __future__ import annotations

import base64
import hashlib
from email import message_from_bytes, policy

import httpx
import pytest

from qlar_email_gateway import client as client_module
from qlar_email_gateway import crypto
from qlar_email_gateway.client import QlarClient, QlarNotAnEndpoint, QlarRejected, QlarUnreachable, Revoked
from qlar_email_gateway.crypto import canonical_job_bytes, canonical_request, verify
from qlar_email_gateway.job_attachments import Fetched, fetch_job_attachments
from qlar_email_gateway.sender import build_reply

JOB_ID = "job_0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
CID = "0123456789abcdef0123456789abcdef@qlar"
HTML = (
    '<p>Hello</p><img style="display:block;max-width:100%;height:auto;border:0;" '
    f'src="cid:{CID}" alt="chart"><p>bye</p>'
)


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


BASE_JOB = {
    "jobId": "job_0123456789abcdef0123456789abcdef",
    "type": "send_email",
    "protocol": 1,
    "to": ["Budi@Customer.test"],
    "subject": "Re: Harga",
    "inReplyTo": "<m1@customer.test>",
    "references": ["<m1@customer.test>"],
    "textBody": "Halo",
    "htmlBody": "<p>Halo</p>",
    "issuedAt": "2026-10-29T10:00:00Z",
    "expiresAt": "2026-10-29T10:10:00Z",
    "agentId": "agt_1",
    "userId": "budi@customer.test",
    "conversationId": "m1@customer.test",
}


def sha_b64(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


class FakeClient:
    """Serves attachment bytes by index; records the paths asked for."""

    def __init__(self, blobs: dict[int, object]) -> None:
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


def job_with(*entries: dict) -> dict:
    return {**BASE_JOB, "protocol": 2, "attachments": list(entries)}


# -- canonical bytes ------------------------------------------------------------------------------


def test_canonical_bytes_without_attachments_unchanged():
    expected = "\n".join(
        [
            "job_0123456789abcdef0123456789abcdef",
            "send_email",
            "1",
            "budi@customer.test",
            "Re: Harga",
            "<m1@customer.test>",
            "<m1@customer.test>",
            sha_b64("Halo"),
            sha_b64("<p>Halo</p>"),
            "2026-10-29T10:00:00Z",
            "2026-10-29T10:10:00Z",
            "agt_1",
            "budi@customer.test",
            "m1@customer.test",
        ]
    ).encode()

    assert canonical_job_bytes(BASE_JOB) == expected
    assert canonical_job_bytes({**BASE_JOB, "attachments": []}) == expected
    assert canonical_job_bytes({**BASE_JOB, "attachments": None}) == expected


def test_canonical_bytes_with_attachments_match_the_server_algorithm():
    first = entry(fileName="a b.png", sizeBytes=1234, sha256="ab" * 32)
    second = entry(fileName="c.jpg", contentType="image/jpeg", contentId=None, sizeBytes=7, sha256="cd" * 32)
    job = job_with(first, second)

    lines = [
        f"a b.png\timage/png\t{CID}\t{'ab' * 32}\t1234",
        f"c.jpg\timage/jpeg\t\t{'cd' * 32}\t7",
    ]
    expected = canonical_job_bytes({**BASE_JOB, "protocol": 2}) + b"\n" + sha_b64("\n".join(lines)).encode()

    assert canonical_job_bytes(job) == expected


def test_missing_null_and_empty_content_id_are_equal_in_canonical_bytes():
    missing = entry()
    del missing["contentId"]
    variants = [missing, entry(contentId=None), entry(contentId="")]

    assert len({canonical_job_bytes(job_with(v)) for v in variants}) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fileName", "other.png"),
        ("contentType", "image/jpeg"),
        ("contentId", "ffffffffffffffffffffffffffffffff@qlar"),
        ("sha256", "00" * 32),
        ("sizeBytes", 1),
    ],
)
def test_tampered_attachment_field_fails_signature(field, value):
    key = crypto.generate_private_key()
    job = job_with(entry())
    job["signature"] = crypto.sign(key, canonical_job_bytes(job))
    pem = crypto.public_key_pem(key)
    assert crypto.verify_job(pem, job)

    job["attachments"][0][field] = value

    assert not crypto.verify_job(pem, job)


def test_dropping_the_attachments_fails_signature():
    key = crypto.generate_private_key()
    job = job_with(entry())
    job["signature"] = crypto.sign(key, canonical_job_bytes(job))

    del job["attachments"]

    assert not crypto.verify_job(crypto.public_key_pem(key), job)


# -- signed GET -----------------------------------------------------------------------------------


def test_get_path_is_signed_relative_to_api_root(monkeypatch):
    key = crypto.generate_private_key()
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    qlar = QlarClient("https://q.test/api/email-gateway/", key, "egw_1")
    path = "/jobs/job_0123456789abcdef0123456789abcdef/attachments/0"

    assert qlar.get_bytes(path, max_bytes=10_000) == PNG
    qlar.get_bytes(path, max_bytes=10_000)

    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == "https://q.test/api/email-gateway" + path
    assert request.content == b""
    canonical = canonical_request(
        "GET", path, request.headers["x-qlar-timestamp"], request.headers["x-qlar-nonce"], b""
    )
    assert verify(key.public_key(), canonical, request.headers["x-qlar-signature"])
    assert request.headers["x-qlar-gateway-id"] == "egw_1"
    assert seen[0].headers["x-qlar-nonce"] != seen[1].headers["x-qlar-nonce"]


def test_get_404_without_json_raises_not_an_endpoint(monkeypatch):
    key = crypto.generate_private_key()
    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kw),
    )

    with pytest.raises(QlarNotAnEndpoint):
        QlarClient("https://q.test/api", key, "egw_1").get_bytes("/jobs/j/attachments/0", max_bytes=10)


# -- fetch_job_attachments ------------------------------------------------------------------------


def test_fetches_and_verifies_each_attachment():
    second = b"JPEGDATA"
    job = job_with(entry(), entry(second, fileName="b.jpg", contentType="image/jpeg", contentId=None))
    client = FakeClient({0: PNG, 1: second})

    fetched = fetch_job_attachments(client, job)

    assert fetched == [
        Fetched("chart.png", "image/png", CID, PNG),
        Fetched("b.jpg", "image/jpeg", None, second),
    ]
    assert client.paths == [f"/jobs/{JOB_ID}/attachments/0", f"/jobs/{JOB_ID}/attachments/1"]


def test_hash_mismatch_discards_attachment():
    job = job_with(entry(), entry(sha256="00" * 32))
    client = FakeClient({0: PNG, 1: PNG})

    assert fetch_job_attachments(client, job) is None


def test_length_mismatch_discards_attachment():
    job = job_with(entry(sizeBytes=len(PNG) + 1))

    assert fetch_job_attachments(FakeClient({0: PNG}), job) is None


def test_upper_case_hash_in_the_job_is_refused():
    job = job_with(entry(sha256=hashlib.sha256(PNG).hexdigest().upper()))

    assert fetch_job_attachments(FakeClient({0: PNG}), job) is None


def test_over_size_discards_attachment():
    big = b"x" * (8 * 1024 * 1024 + 1)
    job = job_with(entry(big))
    client = FakeClient({0: big})

    assert fetch_job_attachments(client, job) is None
    assert client.paths == []


def test_total_over_15mb_is_refused():
    chunk = b"y" * (6 * 1024 * 1024)
    job = job_with(*[entry(chunk, fileName=f"{i}.png") for i in range(3)])

    assert fetch_job_attachments(FakeClient({i: chunk for i in range(3)}), job) is None


def test_more_than_five_entries_refused():
    job = job_with(*[entry(fileName=f"{i}.png") for i in range(6)])
    client = FakeClient({i: PNG for i in range(6)})

    assert fetch_job_attachments(client, job) is None
    assert client.paths == []


def test_404_with_json_returns_none():
    job = job_with(entry())
    client = FakeClient({0: QlarRejected(404, "attachment not found", {"message": "attachment not found"})})

    assert fetch_job_attachments(client, job) is None


def test_404_without_json_body_sends_without_attachments():
    job = job_with(entry())
    client = FakeClient({0: QlarNotAnEndpoint(404, "https://q/x", "")})

    assert fetch_job_attachments(client, job) is None


def test_unreachable_returns_none():
    assert fetch_job_attachments(FakeClient({0: QlarUnreachable("down")}), job_with(entry())) is None


@pytest.mark.parametrize("reason", ["revoked", "unknown_gateway"])
def test_revoked_during_fetch_still_stops(reason):
    client = FakeClient({0: QlarRejected(403, "no", {"reason": reason})})

    with pytest.raises(Revoked):
        fetch_job_attachments(client, job_with(entry()))


def test_malformed_entries_return_none():
    for bad in ({"fileName": "a.png"}, entry(sizeBytes="big"), entry(sizeBytes=True), "text"):
        assert fetch_job_attachments(FakeClient({0: PNG}), job_with(bad)) is None


# -- build_reply ----------------------------------------------------------------------------------


def parse(msg):
    return message_from_bytes(msg.as_bytes(), policy=policy.default)


def test_inline_picture_becomes_related_part_with_content_id(mail):
    job = {**BASE_JOB, "htmlBody": HTML, "protocol": 2, "attachments": [entry()]}

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", CID, PNG)])

    mime = parse(msg)
    assert mime.get_content_type() == "multipart/alternative"
    html_part = mime.get_body(("html",)).get_content()
    assert f'src="cid:{CID}"' in html_part
    related = next(p for p in mime.walk() if p.get_content_type() == "multipart/related")
    image = next(p for p in related.iter_parts() if p.get_content_type() == "image/png")
    assert image["Content-ID"] == f"<{CID}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_filename() == "chart.png"
    assert image.get_payload(decode=True) == PNG


def test_non_inline_becomes_attachment_with_file_name(mail):
    job = {**BASE_JOB, "protocol": 2, "attachments": [entry(contentId=None)]}

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", None, PNG)])

    mime = parse(msg)
    assert mime.get_content_type() == "multipart/mixed"
    (attachment,) = list(mime.iter_attachments())
    assert attachment.get_content_disposition() == "attachment"
    assert attachment.get_filename() == "chart.png"
    assert attachment["Content-ID"] is None
    assert attachment.get_payload(decode=True) == PNG


def test_text_alternative_still_present(mail):
    job = {**BASE_JOB, "htmlBody": HTML, "protocol": 2, "attachments": [entry()]}

    msg = build_reply(mail, job, "<qlar-1@corp.test>", [Fetched("chart.png", "image/png", CID, PNG)])

    assert parse(msg).get_body(("plain",)).get_content().strip() == "Halo"


def test_attachment_fetch_failure_sends_without_cid_images(mail):
    job = {**BASE_JOB, "htmlBody": HTML, "protocol": 2, "attachments": [entry()]}

    msg = build_reply(mail, job, "<qlar-1@corp.test>", None)

    mime = parse(msg)
    assert "cid:" not in msg.as_string()
    assert mime.get_body(("html",)).get_content().count("<img") == 0
    assert "<p>Hello</p>" in mime.get_body(("html",)).get_content()
    assert mime.get_body(("plain",)).get_content().strip() == "Halo"
    assert list(mime.iter_attachments()) == []


def test_job_without_attachments_is_unchanged(mail):
    job = {**BASE_JOB, "htmlBody": HTML}

    msg = build_reply(mail, job, "<qlar-1@corp.test>")

    mime = parse(msg)
    assert mime.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in mime.walk()] == ["multipart/alternative", "text/plain", "text/html"]
    assert f"cid:{CID}" in mime.get_body(("html",)).get_content()  # untouched: nothing was declared


# -- poll -----------------------------------------------------------------------------------------


def _capture_sender(harness):
    calls: list[dict] = []

    def sender(mail, job, **kwargs):
        calls.append({"job": job, **kwargs})
        return "<qlar-abc@corp.test>"

    harness.loop._send = sender
    return calls


def test_poll_fetches_attachments_after_guard_and_passes_them_to_the_sender(harness):
    calls = _capture_sender(harness)
    client = FakeClient({0: PNG})
    harness.loop.client.get_bytes = client.get_bytes
    job = harness.job(protocol=2, jobId=JOB_ID, attachments=[entry()])

    harness.loop._run_job(job)

    assert calls[0]["attachments"] == [Fetched("chart.png", "image/png", CID, PNG)]
    assert harness.results()[0]["status"] == "ok"
    assert [e["status"] for e in harness.audit()] == ["sent"]


def test_poll_fetch_failure_still_sends_and_audits_the_skip(harness):
    calls = _capture_sender(harness)
    harness.loop.client.get_bytes = FakeClient({0: QlarRejected(404, "x", {"message": "x"})}).get_bytes
    job = harness.job(protocol=2, jobId=JOB_ID, attachments=[entry()])

    harness.loop._run_job(job)

    assert calls[0]["attachments"] is None
    assert harness.results()[0]["status"] == "ok"
    audit = harness.audit()
    assert [e["status"] for e in audit] == ["attachments_skipped", "sent"]
    assert "chart" not in str(audit)


def test_poll_guard_refusal_fetches_nothing(harness):
    _capture_sender(harness)
    client = FakeClient({0: PNG})
    harness.loop.client.get_bytes = client.get_bytes
    job = harness.job(protocol=2, jobId=JOB_ID, attachments=[entry()], to=["a@x.test", "b@x.test"])

    harness.loop._run_job(job)

    assert client.paths == []


def test_poll_job_without_attachments_makes_no_extra_request(harness):
    calls = _capture_sender(harness)

    def boom(path, *, max_bytes=None, timeout=30.0):
        raise AssertionError("no fetch expected")

    harness.loop.client.get_bytes = boom
    harness.loop._run_job(harness.job())

    assert "attachments" not in calls[0]


def test_poll_revoked_during_fetch_stops_the_loop(harness):
    _capture_sender(harness)
    harness.loop.client.get_bytes = FakeClient(
        {0: QlarRejected(403, "no", {"reason": "revoked"})}
    ).get_bytes

    with pytest.raises(Revoked):
        harness.loop._run_send_email(harness.job(protocol=2, jobId=JOB_ID, attachments=[entry()]), 0.0)


def test_poll_announces_protocol_2(harness):
    harness.loop._poll_once()

    path, payload = harness.posts[0]
    assert path == "/jobs/poll"
    assert payload["protocol"] == 2
