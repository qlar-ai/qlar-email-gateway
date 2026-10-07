"""Readable text and the /inbound payload (PRD FR-23, FR-24)."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

from qlar_email_gateway.extract import build_inbound_payload, extract_text, strip_quoted_reply

FIXTURES = Path(__file__).parent / "fixtures" / "mail"


def load(name: str) -> EmailMessage:
    with (FIXTURES / name).open("rb") as handle:
        return BytesParser(policy=policy.default).parse(handle)  # type: ignore[return-value]


def plain(body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "budi@customer.test"
    msg["Message-ID"] = "<p@customer.test>"
    msg.set_content(body)
    return msg


def test_prefers_text_plain():
    text, truncated = extract_text(load("multipart-reply.eml"), 20000)

    assert text == "Kalau paket B berapa?"
    assert truncated is False


def test_html_only_drops_style_and_script():
    text, _ = extract_text(load("outlook-html.eml"), 20000)

    assert "color: red" not in text
    assert "alert" not in text
    assert "Berapa harga paket A?" in text
    assert "Terima kasih – Budi" in text  # windows-1252 0x96 is an en dash
    assert "\n\n\n" not in text


def test_latin1_plain_text():
    text, _ = extract_text(load("latin1-plain.eml"), 20000)

    assert text == "Necesito una cotización para el país."


def test_truncates_at_limit_and_flags():
    text, truncated = extract_text(plain("a" * 25000), 20000)

    assert len(text) == 20000
    assert truncated is True


class TestStripQuotedReply:
    def test_quote_lines_removed(self):
        assert strip_quoted_reply("Yes please.\n> Shall we book it?\n> Thanks") == "Yes please."

    def test_english_wrote_line_cuts_the_rest(self):
        text = "Yes please.\n\nOn Wed, 7 Oct 2026 at 15:00, Support <ask@corp.test> wrote:\nShall we book it?"
        assert strip_quoted_reply(text) == "Yes please."

    def test_indonesian_menulis_line_cuts_the_rest(self):
        text = "Baik.\nPada Rab, 7 Okt 2026 15.00, Support <ask@corp.test> menulis:\nApakah jadi?"
        assert strip_quoted_reply(text) == "Baik."

    def test_original_message_marker_cuts_the_rest(self):
        assert strip_quoted_reply("Ok.\n-----Original Message-----\nFrom: x") == "Ok."

    def test_outlook_header_block_cuts_the_rest(self):
        text = (
            "Setuju.\n\nFrom: Support <ask@corp.test>\n"
            "Sent: Wednesday, October 7, 2026 3:00 PM\nTo: Budi\nOld text"
        )
        assert strip_quoted_reply(text) == "Setuju."

    def test_indonesian_outlook_header_block(self):
        text = "Setuju.\nDari: Support\nDikirim: Rabu\nKepada: Budi\nOld"
        assert strip_quoted_reply(text) == "Setuju."

    def test_signature_separator_cuts_the_rest(self):
        assert strip_quoted_reply("Thanks.\n-- \nBudi Santoso\nCEO") == "Thanks."

    def test_from_line_without_sent_line_is_kept(self):
        text = "From: the warehouse, three boxes arrived.\nPlease confirm."
        assert strip_quoted_reply(text) == text

    def test_only_a_quote_keeps_the_original(self):
        # Never forward empty when the sender wrote something.
        text = "> just forwarding this\n> to you"
        assert strip_quoted_reply(text) == text


def test_payload_fields():
    payload = build_inbound_payload(load("multipart-reply.eml"), uid=4183, limit=20000)

    assert payload["messageId"] == "<m2@customer.test>"
    assert payload["uid"] == 4183
    assert payload["from"] == {"address": "budi@customer.test", "name": "Budi Santoso"}
    assert payload["to"] == [
        {"address": "ask@corp.test", "name": "Support"},
        {"address": "other@corp.test", "name": ""},
    ]
    assert payload["cc"] == [{"address": "ani@customer.test", "name": "Ani"}]
    assert payload["replyTo"] == "team@customer.test"
    assert payload["subject"] == "Re: Harga paket"
    assert payload["date"] == "2026-10-07T09:00:00Z"
    assert payload["inReplyTo"] == "<qlar-1@corp.test>"
    assert payload["references"] == ["<root@customer.test>", "<qlar-1@corp.test>"]
    assert payload["text"] == "Kalau paket B berapa?"
    assert payload["textTruncated"] is False
    assert payload["attachments"] == []
    assert payload["headers"] == {"autoSubmitted": "no", "precedence": "normal", "listId": None}


def test_payload_without_optional_headers():
    payload = build_inbound_payload(plain("Halo"), uid=1, limit=20000)

    assert payload["replyTo"] is None
    assert payload["inReplyTo"] is None
    assert payload["references"] == []
    assert payload["date"] is None
    assert payload["subject"] == ""
    assert payload["to"] == []
