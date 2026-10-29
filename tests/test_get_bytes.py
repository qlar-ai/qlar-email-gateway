"""The signed GET streams the body and enforces its size limit while downloading."""

from __future__ import annotations

import httpx
import pytest

from qlar_email_gateway import client as client_module
from qlar_email_gateway import crypto
from qlar_email_gateway.client import QlarClient, QlarRejected, QlarResponseTooLarge

PATH = "/jobs/job_0123456789abcdef0123456789abcdef/attachments/0"


def _serve(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    return QlarClient("https://q.test/api/email-gateway", crypto.generate_private_key(), "egw_1")


def test_get_bytes_aborts_when_body_exceeds_max(monkeypatch):
    pulled: list[int] = []

    def chunks():
        for index in range(100):
            pulled.append(index)
            yield b"x" * 600

    qlar = _serve(monkeypatch, lambda request: httpx.Response(200, content=chunks()))

    with pytest.raises(QlarResponseTooLarge):
        qlar.get_bytes(PATH, max_bytes=1000)

    assert len(pulled) <= 3


def test_get_bytes_rejects_declared_content_length_over_max_without_reading(monkeypatch):
    pulled: list[int] = []

    def chunks():
        pulled.append(1)
        yield b"x" * 100

    qlar = _serve(
        monkeypatch,
        lambda request: httpx.Response(200, content=chunks(), headers={"content-length": "5000000"}),
    )

    with pytest.raises(QlarResponseTooLarge):
        qlar.get_bytes(PATH, max_bytes=1000)

    assert pulled == []


def test_binary_200_is_not_json_decoded(monkeypatch):
    body = b"\x89PNG\r\n\x1a\n{not json}"
    qlar = _serve(monkeypatch, lambda request: httpx.Response(200, content=body))

    def forbidden(self, *args, **kwargs):
        raise AssertionError("a binary body must not be decoded")

    monkeypatch.setattr(httpx.Response, "json", forbidden)
    monkeypatch.setattr(httpx.Response, "text", property(forbidden))

    assert qlar.get_bytes(PATH, max_bytes=1000) == body


def test_exactly_max_bytes_is_accepted(monkeypatch):
    qlar = _serve(monkeypatch, lambda request: httpx.Response(200, content=b"y" * 1000))

    assert qlar.get_bytes(PATH, max_bytes=1000) == b"y" * 1000


def test_error_body_is_capped_and_still_classified(monkeypatch):
    big_json_tail = b'{"reason":"revoked","pad":"' + b"z" * 100_000 + b'"}'
    qlar = _serve(monkeypatch, lambda request: httpx.Response(403, content=big_json_tail))

    with pytest.raises(QlarRejected) as raised:
        qlar.get_bytes(PATH, max_bytes=1000)

    assert raised.value.status == 403


def test_error_body_with_json_reason_is_kept(monkeypatch):
    qlar = _serve(monkeypatch, lambda request: httpx.Response(403, json={"reason": "revoked"}))

    with pytest.raises(QlarRejected) as raised:
        qlar.get_bytes(PATH, max_bytes=1000)

    assert raised.value.body == {"reason": "revoked"}
