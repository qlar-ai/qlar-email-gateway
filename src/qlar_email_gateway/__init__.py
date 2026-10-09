"""Qlar Email Gateway — on-premise email gateway.

The gateway runs inside the customer's network next to their mailbox. It watches the inbox
over IMAP and forwards each human email to Qlar over an outbound HTTPS connection, then pulls
signed reply jobs from Qlar and sends them over SMTP from the same mailbox. No inbound port is
opened, and the mailbox credentials never leave the customer's premises.
"""

# Single source of truth for the release version: pyproject.toml reads it from here
# (`[tool.setuptools.dynamic] version = { attr = "qlar_email_gateway.__version__" }`), so a
# release only ever has to change this line plus CHANGELOG.md.
__version__ = "0.1.2"

# The wire contract version, deliberately separate from the release version. A bug-fix release
# must NOT bump this — only an incompatible protocol change does.
PROTOCOL_VERSION = 1

__all__ = ["__version__", "PROTOCOL_VERSION"]
