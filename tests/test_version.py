"""`version` is what support asks for first; one line, release and protocol."""

from __future__ import annotations

from qlar_email_gateway import __version__, cli


def test_version_prints_release_and_protocol(capsys):
    assert cli.main(["version"]) == 0

    assert capsys.readouterr().out.strip() == f"qlar-email-gateway {__version__} (protocol 1)"
