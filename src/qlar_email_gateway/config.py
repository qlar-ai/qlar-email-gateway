"""Settings and persisted enrolment state.

Two files, with deliberately different lifetimes:

* `.env` — written by the operator, or by the setup prompts on first run (see
  `wizard.py`). Qlar's URL, the one-time enrolment code, and the mailbox credentials.
  **The mailbox password lives here and only here**; it is never sent to Qlar.
* `gateway-state.json` — written by the gateway. The gateway id Qlar issued and Qlar's public
  key (pinned at enrolment so later jobs can be verified), plus what the mailbox watcher and
  the send guard must remember across restarts: the IMAP `UIDVALIDITY`/last forwarded UID and
  who recently wrote in.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_POLL_TIMEOUT_SECONDS = 25
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_RECIPIENT_MEMORY_DAYS = 30
DEFAULT_MAX_SENDS_PER_HOUR_PER_RECIPIENT = 20
DEFAULT_MAX_INBOUND_TEXT_CHARS = 20000

#: How many jobs run at once. Not configurable: replies are small and one SMTP connection per
#: job is plenty; two lets a slow send not hold up a connection test.
MAX_CONCURRENT_JOBS = 2

SECURITY_MODES = ("ssl", "starttls")

#: Serialises every write of gateway-state.json. Two threads write it — the mailbox watcher
#: (last UID, recipients) and the poll loop (nothing yet, but the send guard lives there) — and
#: an interleaved read-modify-write would silently drop a recipient or rewind the UID.
STATE_LOCK = threading.Lock()


class ConfigError(Exception):
    """The gateway cannot start with the configuration it was given."""


def load_dotenv(path: Path) -> None:
    """Loads `KEY=value` lines into the environment without overwriting real env vars.

    Hand-rolled rather than pulling in python-dotenv: the format we need is three lines of
    parsing, and every dependency in this process is one more thing a customer's security
    review has to cover. Real environment variables win, so a container can override the
    file without editing it.
    """
    if not path.exists():
        return

    for raw_line in read_env_text(path).splitlines():
        line = unwrap_quoted_line(raw_line.strip())
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = unquote_env_value(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


def read_env_text(path: Path) -> str:
    """Reads an env file, naming the encoding problems Windows creates rather than crashing.

    Two of them, both from an operator following instructions on the wrong shell:

    * **UTF-16.** PowerShell 5.1's `>>` writes it. Decoded as UTF-8 the file is either a
      `UnicodeDecodeError` traceback or, worse, silence - so it is detected and explained.
    * **A UTF-8 BOM.** Several Windows editors add one, and it would otherwise become part
      of the first key, which then matches nothing and is invisible on screen.
    """
    raw = path.read_bytes()

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or raw[1:2] == b"\x00":
        raise ConfigError(
            f"{path} is UTF-16 encoded, which usually means it was written by PowerShell's "
            "`>>` operator. Save it as UTF-8, or delete it and let the setup prompts write it."
        )

    try:
        # utf-8-sig drops a BOM when there is one and behaves as plain UTF-8 when there is not.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConfigError(
            f"{path} is not valid UTF-8 ({error}). Save it as UTF-8, or delete it and let the "
            "setup prompts write it."
        ) from error


def unwrap_quoted_line(line: str) -> str:
    """Unwraps a whole `'KEY=value'` line, which is what cmd.exe writes.

    `echo 'KEY=value' >> .env` is correct in a POSIX shell and a trap in cmd, which has no
    single-quote quoting and writes the quotes literally. The result is a key named `'KEY`
    that matches nothing, while the line on screen looks exactly right — the operator sees
    their setting in the file and the gateway says it is unset.

    Unwrapping costs nothing: a value that is itself quoted, `KEY='value'`, does not start
    with a quote, so it is left to `unquote_env_value` as before.
    """
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'" and "=" in line[1:-1]:
        return line[1:-1].strip()
    return line


def unquote_env_value(value: str) -> str:
    """Removes one matching pair of surrounding quotes, and only one.

    Quoting is how a password with a leading space, a trailing space or a `#` survives the
    round trip through this file; stripping every quote character instead would corrupt a
    password that legitimately ends in one.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def quote_env_value(value: str) -> str:
    """Formats a value for a `KEY=value` line, quoting it when it would not survive plain.

    The counterpart of `unquote_env_value`. Anything that the loader's `strip()` would eat,
    that a human would read as the start of a comment, or that a shell would split on gets
    wrapped in double quotes. A space inside a password survives either way here, but the
    line is regularly copied into a terminal, where it would not.
    """
    if value == "":
        return ""
    needs_quotes = (
        any(character.isspace() for character in value)
        or "#" in value
        or (value[0] in "\"'" and value[-1] == value[0])
    )
    return f'"{value}"' if needs_quotes else value


def write_env_values(path: Path, values: dict[str, str]) -> Path | None:
    """Writes `KEY=value` settings into an env file, updating keys where they already are.

    Rewriting the file rather than appending keeps it readable after the tenth `--init`:
    a key that is already there is replaced in place, comments and hand-added settings are
    left exactly as they were, and only genuinely new keys are appended.

    The file holds a database password, so it is created with mode 0600 already set — the
    same reasoning as the private key in `crypto.py`. It is truncated and rewritten in
    place rather than written beside and renamed: a `.env` is routinely bind-mounted into
    a container by path, and a rename would leave the container holding the old inode.

    Returns the path an unreadable existing file was moved to, or None. A file this cannot
    parse is never silently overwritten — whatever is in it was put there by someone.
    """
    backup: Path | None = None
    lines = _new_env_file_header()

    if path.exists():
        try:
            lines = read_env_text(path).splitlines()
        except ConfigError:
            backup = path.with_suffix(path.suffix + ".bak")
            path.replace(backup)

    remaining = dict(values)
    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                rewritten.append(f"{key}={quote_env_value(remaining.pop(key))}")
                continue
        rewritten.append(line)

    if remaining:
        if rewritten and rewritten[-1].strip():
            rewritten.append("")
        rewritten.extend(f"{key}={quote_env_value(value)}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(rewritten) + "\n")

    # os.open only applies the mode when it creates the file; an existing .env written by
    # hand may be world-readable, and it now contains a password either way.
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows, or a filesystem without modes
        pass

    return backup


def _new_env_file_header() -> list[str]:
    """The comment block a freshly generated `.env` starts with.

    Deliberately short. The settings the prompts ask about are appended below it, and the
    handful worth knowing exist are listed commented-out so that the file answers "what
    else can I set here?" without anyone having to find `.env.example` again.
    """
    return [
        "# Qlar Email Gateway - configuration",
        "#",
        "# Generated by `qlar-email-gateway --init`. Real environment variables take precedence",
        "# over this file, so a container or systemd unit can override any line below.",
        "#",
        "# This file contains your mailbox password: keep it mode 0600 and out of version",
        "# control. It is never sent to Qlar. Re-run `qlar-email-gateway run --init` to change",
        "# these answers.",
        "#",
        "# Other settings, with their defaults - uncomment to change one:",
        "#QLAR_GATEWAY_NAME=            # label shown in the CMS; defaults to the hostname",
        "#QLAR_ENROLLMENT_CODE=         # one-time code from the CMS, only needed for `enroll`",
        "#IMAP_FOLDER=INBOX",
        "#POLL_INTERVAL_SECONDS=60      # only used when the server has no IDLE",
        "#RECIPIENT_ALLOWLIST=          # addresses or domains that may always be emailed",
        "#RECIPIENT_MEMORY_DAYS=30",
        "#MAX_SENDS_PER_HOUR_PER_RECIPIENT=20",
        "#MAX_INBOUND_TEXT_CHARS=20000",
        "#AUDIT_LOG_FILE=./audit/mail.jsonl",
        "#GATEWAY_KEY_FILE=./gateway-key.pem",
        "#GATEWAY_STATE_FILE=./gateway-state.json",
        "#LOG_LEVEL=INFO",
        "",
    ]


@dataclass(frozen=True)
class MailSettings:
    """The mailbox. `password` is used for the IMAP and SMTP login on this machine only."""

    imap_host: str
    smtp_host: str
    user: str
    password: str = field(repr=False)
    address: str
    imap_port: int = 993
    imap_security: Literal["ssl", "starttls"] = "ssl"
    imap_folder: str = "INBOX"
    smtp_port: int = 587
    smtp_security: Literal["ssl", "starttls"] = "starttls"
    from_name: str = ""


@dataclass(frozen=True)
class Settings:
    base_url: str
    gateway_name: str
    enrollment_code: str | None
    key_file: Path
    state_file: Path
    audit_log_file: Path | None
    poll_timeout_seconds: int
    verify_tls: bool
    mail: MailSettings
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    recipient_allowlist: tuple[str, ...] = ()
    recipient_memory_days: int = DEFAULT_RECIPIENT_MEMORY_DAYS
    max_sends_per_hour_per_recipient: int = DEFAULT_MAX_SENDS_PER_HOUR_PER_RECIPIENT
    max_inbound_text_chars: int = DEFAULT_MAX_INBOUND_TEXT_CHARS


@dataclass
class EnrollmentState:
    """What the gateway learned at enrolment and has seen since, persisted across restarts."""

    gateway_id: str
    qlar_public_key_pem: str
    enrolled_at: str
    base_url: str
    mailbox_address: str = ""
    uid_validity: int | None = None
    last_uid: int | None = None
    #: Lowercased address -> ISO time it last wrote in. The send guard's memory (FR-33).
    recipients: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> EnrollmentState | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            gateway_id=data["gatewayId"],
            qlar_public_key_pem=data["qlarPublicKeyPem"],
            enrolled_at=data["enrolledAt"],
            base_url=data["baseUrl"],
            # Absent in a state file written before the field existed; tolerated, not an error.
            mailbox_address=data.get("mailboxAddress", ""),
            uid_validity=data.get("uidValidity"),
            last_uid=data.get("lastUid"),
            recipients=dict(data.get("recipients") or {}),
        )

    def save(self, path: Path) -> None:
        """Writes the whole state atomically: a crash mid-write must never leave half a file,
        because this file is the gateway's identity and its place in the mailbox."""
        payload = json.dumps(
            {
                "gatewayId": self.gateway_id,
                "qlarPublicKeyPem": self.qlar_public_key_pem,
                "enrolledAt": self.enrolled_at,
                "baseUrl": self.base_url,
                "mailboxAddress": self.mailbox_address,
                "uidValidity": self.uid_validity,
                "lastUid": self.last_uid,
                "recipients": self.recipients,
            },
            indent=2,
        )
        with STATE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_security(name: str, default: str) -> Literal["ssl", "starttls"]:
    value = (os.environ.get(name) or "").strip().lower() or default
    if value not in SECURITY_MODES:
        raise ConfigError(f"{name} must be ssl or starttls, got {value!r}")
    return value  # type: ignore[return-value]


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.environ.get(name, "").split(",") if item.strip())


def load_settings(env_file: Path | None = None) -> Settings:
    """Builds the settings, raising ConfigError with an actionable message on any gap."""
    if env_file is not None:
        load_dotenv(env_file)

    base_url = os.environ.get("QLAR_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError(
            "QLAR_BASE_URL is required. The Qlar CMS email panel shows it; it ends in "
            "/api/email-gateway"
        )
    if not base_url.startswith("https://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        raise ConfigError("QLAR_BASE_URL must use https:// outside local testing")

    missing = [
        name for name in ("IMAP_HOST", "SMTP_HOST", "MAIL_USER", "MAIL_PASSWORD") if not os.environ.get(name)
    ]
    if missing:
        raise ConfigError(f"missing required mailbox settings: {', '.join(missing)}")

    user = os.environ["MAIL_USER"].strip()
    mail = MailSettings(
        imap_host=os.environ["IMAP_HOST"].strip(),
        imap_port=_env_int("IMAP_PORT", 993),
        imap_security=_env_security("IMAP_SECURITY", "ssl"),
        imap_folder=(os.environ.get("IMAP_FOLDER") or "").strip() or "INBOX",
        smtp_host=os.environ["SMTP_HOST"].strip(),
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_security=_env_security("SMTP_SECURITY", "starttls"),
        user=user,
        password=os.environ["MAIL_PASSWORD"],
        address=((os.environ.get("MAIL_ADDRESS") or "").strip() or user).lower(),
        from_name=(os.environ.get("MAIL_FROM_NAME") or "").strip(),
    )

    audit_raw = os.environ.get("AUDIT_LOG_FILE", "./audit/mail.jsonl").strip()

    return Settings(
        base_url=base_url,
        gateway_name=os.environ.get("QLAR_GATEWAY_NAME", "").strip() or _default_gateway_name(),
        enrollment_code=(os.environ.get("QLAR_ENROLLMENT_CODE") or "").strip() or None,
        key_file=Path(os.environ.get("GATEWAY_KEY_FILE", "./gateway-key.pem")).expanduser(),
        state_file=Path(os.environ.get("GATEWAY_STATE_FILE", "./gateway-state.json")).expanduser(),
        audit_log_file=Path(audit_raw).expanduser() if audit_raw else None,
        poll_timeout_seconds=_env_int("POLL_TIMEOUT_SECONDS", DEFAULT_POLL_TIMEOUT_SECONDS),
        # Only ever disabled for testing against a local Qlar with a self-signed cert;
        # the flag is named so that it reads as a mistake in a production .env.
        verify_tls=_env_bool("QLAR_INSECURE_SKIP_TLS_VERIFY", False) is False,
        mail=mail,
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS),
        recipient_allowlist=_env_list("RECIPIENT_ALLOWLIST"),
        recipient_memory_days=_env_int("RECIPIENT_MEMORY_DAYS", DEFAULT_RECIPIENT_MEMORY_DAYS),
        max_sends_per_hour_per_recipient=_env_int(
            "MAX_SENDS_PER_HOUR_PER_RECIPIENT", DEFAULT_MAX_SENDS_PER_HOUR_PER_RECIPIENT
        ),
        max_inbound_text_chars=_env_int("MAX_INBOUND_TEXT_CHARS", DEFAULT_MAX_INBOUND_TEXT_CHARS),
    )


def _default_gateway_name() -> str:
    import socket

    return f"{socket.gethostname()} gateway"
