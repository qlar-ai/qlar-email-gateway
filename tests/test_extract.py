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


GMAIL_FORWARD = (
    "Tolong dijawab ya.\n\n"
    "---------- Forwarded message ---------\n"
    "From: Andri <andri@customer.test>\n"
    "Date: Wed, 7 Oct 2026 at 15:00\n"
    "Subject: Harga paket\n"
    "To: Yanto <yanto@customer.test>\n\n"
    "Berapa harga paket A?\n"
)


def forward(body: str, subject: str = "Fwd: Harga paket") -> EmailMessage:
    msg = plain(body)
    msg["Subject"] = subject
    return msg


class TestForward:
    def test_gmail_forward_keeps_the_forwarded_message(self):
        text, _ = extract_text(forward(GMAIL_FORWARD), 20000)

        assert "Tolong dijawab ya." in text
        assert "Andri <andri@customer.test>" in text
        assert "Berapa harga paket A?" in text

    def test_forward_without_own_words_keeps_the_forwarded_message(self):
        body = GMAIL_FORWARD.replace("Tolong dijawab ya.\n\n", "")
        text, _ = extract_text(forward(body), 20000)

        assert "Berapa harga paket A?" in text

    def test_outlook_forward_by_subject_keeps_the_header_block(self):
        body = "FYI\n\nFrom: Andri\nSent: Wednesday\nTo: Yanto\nSubject: Harga\n\nBerapa harga paket A?"
        text, _ = extract_text(forward(body, "FW: Harga"), 20000)

        assert "Berapa harga paket A?" in text

    def test_apple_banner_without_forward_subject(self):
        body = "Cek ini\n\nBegin forwarded message:\n\nFrom: Andri\nDate: Rabu\nSubject: Harga\n\nBerapa?"
        text, _ = extract_text(forward(body, "Harga"), 20000)

        assert "Berapa?" in text

    def test_quote_marks_of_an_inline_forward_are_removed(self):
        body = "Cek\n\n---------- Pesan terusan ----------\n> Dari: Andri\n> Berapa?"
        text, _ = extract_text(forward(body, "Harga"), 20000)

        assert "Dari: Andri\nBerapa?" in text

    def test_reply_in_a_thread_that_has_fwd_in_the_subject_is_still_cut(self):
        body = "Paket B?\n\nOn Wed, 7 Oct 2026 at 15:00, Qlar <ask@corp.test> wrote:\nPaket A Rp 1 juta."
        text, _ = extract_text(forward(body, "Re: Fwd: Harga paket"), 20000)

        assert text == "Paket B?"

    def test_old_forward_banner_under_a_reply_header_is_still_cut(self):
        body = (
            "Paket B?\n\nOn Wed, 7 Oct 2026 at 15:00, Qlar <ask@corp.test> wrote:\n"
            "---------- Forwarded message ---------\nFrom: Andri\nDate: x\nold"
        )
        text, _ = extract_text(forward(body, "Re: Harga"), 20000)

        assert text == "Paket B?"


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
    assert payload["headers"] == {
        "autoSubmitted": "no",
        "precedence": "normal",
        "listId": None,
        "authenticationResults": None,
    }


class TestAuthenticationResults:
    def test_topmost_header_is_forwarded_on_one_line(self):
        raw = (
            b"From: budi@customer.test\r\nMessage-ID: <p@customer.test>\r\n"
            b"Authentication-Results: mx.google.com;\r\n dkim=pass header.i=@corp.test;\r\n"
            b" dmarc=pass header.from=corp.test\r\n\r\nHalo\r\n"
        )
        msg = BytesParser(policy=policy.default).parsebytes(raw)

        headers = build_inbound_payload(msg, uid=1, limit=20000)["headers"]  # type: ignore[arg-type]

        assert headers["authenticationResults"] == (
            "mx.google.com; dkim=pass header.i=@corp.test; dmarc=pass header.from=corp.test"
        )

    def test_only_the_first_of_several_headers_is_read(self):
        msg = plain("Halo")
        msg["Authentication-Results"] = "mx.google.com; dmarc=fail header.from=corp.test"
        msg["Authentication-Results"] = "forged.example; dmarc=pass header.from=corp.test"

        headers = build_inbound_payload(msg, uid=1, limit=20000)["headers"]

        assert headers["authenticationResults"] == "mx.google.com; dmarc=fail header.from=corp.test"

    def test_missing_header_is_null(self):
        headers = build_inbound_payload(plain("Halo"), uid=1, limit=20000)["headers"]

        assert headers["authenticationResults"] is None

    def test_very_long_header_is_capped(self):
        msg = plain("Halo")
        msg["Authentication-Results"] = "mx.google.com; " + "x" * 5000

        headers = build_inbound_payload(msg, uid=1, limit=20000)["headers"]

        assert len(headers["authenticationResults"]) == 2000


def test_payload_without_optional_headers():
    payload = build_inbound_payload(plain("Halo"), uid=1, limit=20000)

    assert payload["replyTo"] is None
    assert payload["inReplyTo"] is None
    assert payload["references"] == []
    assert payload["date"] is None
    assert payload["subject"] == ""
    assert payload["to"] == []
