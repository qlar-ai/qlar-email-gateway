"""`version` is what support asks for first; one line, release and protocol."""

from __future__ import annotations

from qlar_email_gateway import PROTOCOL_VERSION, __version__, cli


def test_version_prints_release_and_protocol(capsys):
    assert cli.main(["version"]) == 0

    assert capsys.readouterr().out.strip() == "qlar-email-gateway 0.2.0 (protocol 2)"


def test_release_and_protocol_constants():
    assert __version__ == "0.2.0"
    assert PROTOCOL_VERSION == 2
