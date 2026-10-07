"""`version` is what support asks for first; one line, release and protocol."""

from __future__ import annotations

from qlar_email_gateway import cli


def test_version_prints_release_and_protocol(capsys):
    assert cli.main(["version"]) == 0

    assert capsys.readouterr().out.strip() == "qlar-email-gateway 0.1.0 (protocol 1)"
