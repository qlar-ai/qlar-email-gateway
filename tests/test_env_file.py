"""The `.env` file is now written by the gateway, not only read by it.

Which makes its round trip a correctness problem rather than a formatting preference: a
password that survives being asked for but not being read back would leave an operator
staring at an authentication failure for a credential they typed correctly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qlar_email_gateway.config import load_dotenv, quote_env_value, unquote_env_value, write_env_values


def round_trip(value: str) -> str:
    return unquote_env_value(quote_env_value(value))


class TestValuesSurviveTheRoundTrip:
    @pytest.mark.parametrize(
        "password",
        [
            "simple",
            "with space",
            " leading",
            "trailing ",
            "hash#inside",
            "#leading-hash",
            'ends-with-a-quote"',
            '"fully quoted"',
            "'single'",
            "@UFp57nW1q2%Hbzc",
            "equals=sign",
            "back\\slash",
        ],
    )
    def test_password_reads_back_exactly(self, password):
        assert round_trip(password) == password

    def test_empty_value_stays_empty(self):
        assert round_trip("") == ""


class TestWritingSettings:
    def test_creates_the_file_with_a_header_and_the_values(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        write_env_values(env_file, {"DB_HOST": "db.internal", "DB_PASSWORD": "p@ss word"})

        text = env_file.read_text(encoding="utf-8")
        assert text.startswith("# Qlar Email Gateway")
        assert "DB_HOST=db.internal" in text
        assert 'DB_PASSWORD="p@ss word"' in text

        for key in ("DB_HOST", "DB_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        load_dotenv(env_file)
        assert os.environ["DB_HOST"] == "db.internal"
        assert os.environ["DB_PASSWORD"] == "p@ss word"

    def test_updates_keys_in_place_and_leaves_everything_else_alone(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "# my own notes",
                    "DB_HOST=old.internal",
                    "",
                    "#TABLE_ALLOWLIST=public.orders",
                    "MAX_CONCURRENT_QUERIES=9",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        write_env_values(env_file, {"DB_HOST": "new.internal", "DB_NAME": "warehouse"})

        lines = env_file.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# my own notes"
        assert "DB_HOST=new.internal" in lines
        assert "DB_HOST=old.internal" not in lines
        # A commented-out setting is a note to self, not a value to overwrite.
        assert "#TABLE_ALLOWLIST=public.orders" in lines
        assert "MAX_CONCURRENT_QUERIES=9" in lines
        # A key that was not there yet is appended rather than dropped.
        assert "DB_NAME=warehouse" in lines

    def test_rewriting_twice_does_not_duplicate_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_values(env_file, {"DB_HOST": "one.internal"})
        write_env_values(env_file, {"DB_HOST": "two.internal"})

        lines = env_file.read_text(encoding="utf-8").splitlines()
        assert [line for line in lines if line.startswith("DB_HOST=")] == ["DB_HOST=two.internal"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
    def test_the_file_holding_the_password_is_not_world_readable(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_values(env_file, {"DB_PASSWORD": "secret"})
        assert Path(env_file).stat().st_mode & 0o077 == 0
