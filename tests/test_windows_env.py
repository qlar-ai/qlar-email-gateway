"""The three ways a `.env` arrives broken from a Windows machine.

Every case here came from one operator, in one sitting, following the instructions the CMS
printed. None of them is exotic: the instructions were POSIX shell, the machine was not,
and the gateway's answer was "QLAR_ENROLLMENT_CODE is not set" about a file that plainly
contained `QLAR_ENROLLMENT_CODE`.

A config file that silently ignores what it was told is worse than one that refuses to
load, because the operator has no way in. So the reader now either understands the file or
says precisely what is wrong with it.
"""

from __future__ import annotations

import os

import pytest

from qlar_email_gateway.config import ConfigError, load_dotenv, read_env_text, write_env_values

BASE_URL = "https://qlar.example.com/api/email-gateway"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in ("QLAR_BASE_URL", "QLAR_ENROLLMENT_CODE", "DB_HOST"):
        monkeypatch.delenv(key, raising=False)


class TestCmdExeQuoting:
    """`echo 'KEY=value' >> .env` in cmd.exe writes the quotes into the file."""

    def test_a_whole_line_wrapped_in_quotes_is_still_read(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_bytes(
            f"'QLAR_BASE_URL={BASE_URL}' \r\n'QLAR_ENROLLMENT_CODE=3GSL-D4GD-V7YY' \r\n".encode()
        )

        load_dotenv(env_file)

        assert os.environ["QLAR_BASE_URL"] == BASE_URL
        assert os.environ["QLAR_ENROLLMENT_CODE"] == "3GSL-D4GD-V7YY"

    def test_double_quotes_too(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(f'"QLAR_BASE_URL={BASE_URL}"\n', encoding="utf-8")

        load_dotenv(env_file)

        assert os.environ["QLAR_BASE_URL"] == BASE_URL

    def test_a_quoted_value_is_still_just_a_quoted_value(self, tmp_path):
        # The line does not start with a quote, so this is the ordinary case and the value
        # keeps its meaning: quoting is how a password with spaces survives the file.
        env_file = tmp_path / ".env"
        env_file.write_text('DB_HOST="db.internal"\n', encoding="utf-8")

        load_dotenv(env_file)

        assert os.environ["DB_HOST"] == "db.internal"


class TestEncodings:
    def test_a_utf8_bom_does_not_become_part_of_the_first_key(self, tmp_path):
        # Notepad and several Windows editors write one. Without this the first key is
        # "﻿QLAR_BASE_URL", which matches nothing and looks identical on screen.
        env_file = tmp_path / ".env"
        env_file.write_bytes(b"\xef\xbb\xbf" + f"QLAR_BASE_URL={BASE_URL}\n".encode())

        load_dotenv(env_file)

        assert os.environ["QLAR_BASE_URL"] == BASE_URL

    def test_utf16_is_named_rather_than_crashed_on(self, tmp_path):
        # PowerShell 5.1's `>>` writes UTF-16. Decoded as UTF-8 this used to raise
        # UnicodeDecodeError out of a config loader, as a traceback.
        env_file = tmp_path / ".env"
        env_file.write_bytes(f"QLAR_BASE_URL={BASE_URL}\n".encode("utf-16"))

        with pytest.raises(ConfigError) as caught:
            load_dotenv(env_file)

        assert "UTF-16" in str(caught.value)
        assert "PowerShell" in str(caught.value)

    def test_other_invalid_bytes_are_named_too(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_bytes(b"QLAR_BASE_URL=caf\xe9\n")

        with pytest.raises(ConfigError) as caught:
            read_env_text(env_file)

        assert "not valid UTF-8" in str(caught.value)


class TestWritingOverAnUnreadableFile:
    def test_the_old_file_is_kept_rather_than_overwritten(self, tmp_path):
        env_file = tmp_path / ".env"
        original = f"QLAR_BASE_URL={BASE_URL}\n".encode("utf-16")
        env_file.write_bytes(original)

        backup = write_env_values(env_file, {"DB_HOST": "db.internal"})

        assert backup is not None
        assert backup.read_bytes() == original, "whatever was in there, someone put it there"
        assert "DB_HOST=db.internal" in env_file.read_text(encoding="utf-8")

    def test_a_readable_file_is_updated_in_place_as_before(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# mine\nDB_HOST=old.internal\n", encoding="utf-8")

        backup = write_env_values(env_file, {"DB_HOST": "new.internal"})

        assert backup is None
        text = env_file.read_text(encoding="utf-8")
        assert "# mine" in text
        assert "DB_HOST=new.internal" in text


class TestSayingWhatWasRead:
    """Being asked for something already written down is infuriating without a reason.

    An operator echoed QLAR_BASE_URL into `.env`, ran the gateway, and was asked for the
    Qlar endpoint anyway. The gateway was right — it had not read the file — but nothing on
    screen said so, and the three causes (wrong directory, unreadable encoding, not a
    setting) look identical from the outside.
    """

    def test_it_names_the_keys_it_understood(self, tmp_path):
        from qlar_email_gateway.wizard import _describe_existing

        env_file = tmp_path / ".env"
        env_file.write_text(f"# a comment\nQLAR_BASE_URL={BASE_URL}\nDB_PASSWORD=hunter2\n", encoding="utf-8")

        described = _describe_existing(env_file)

        assert "QLAR_BASE_URL" in described
        assert "DB_PASSWORD" in described
        # Names, never values: one of those keys is a database password.
        assert "hunter2" not in described

    def test_it_says_when_there_is_no_file(self, tmp_path):
        from qlar_email_gateway.wizard import _describe_existing

        assert "no file there yet" in _describe_existing(tmp_path / ".env")

    def test_it_repeats_the_encoding_complaint(self, tmp_path):
        from qlar_email_gateway.wizard import _describe_existing

        env_file = tmp_path / ".env"
        env_file.write_bytes(f"QLAR_BASE_URL={BASE_URL}\n".encode("utf-16"))

        assert "UTF-16" in _describe_existing(env_file)

    def test_a_file_with_nothing_in_it_says_so(self, tmp_path):
        from qlar_email_gateway.wizard import _describe_existing

        env_file = tmp_path / ".env"
        env_file.write_text("# only comments here\n\n", encoding="utf-8")

        assert "no settings in it" in _describe_existing(env_file)
