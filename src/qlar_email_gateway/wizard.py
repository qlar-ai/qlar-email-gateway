"""First-run setup: ask for the mailbox details, write them to `.env`, prove they work.

Installing the gateway and starting it should be the whole job. Copying `.env.example`,
remembering which variable names the loader expects, and discovering a typo hours later when the
first reply fails to send is work that a handful of prompts can do instead.

So a start with no usable configuration asks, writes the answers to `.env`, and logs in to IMAP
and SMTP once before going any further. The answers are written to the file precisely so that
this happens exactly once: the next start — a restart, a systemd unit, a replaced container —
reads the file and never asks again. `--init` asks anyway, for the day the password rotates or
the mail server moves.

None of this is mandatory. A `.env` written by hand, or environment variables set by a container,
still take precedence and the prompts never appear; and without a terminal to ask on, the
configuration error is printed as before. The prompts are a convenience for a human at a console,
never a new requirement for automation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import console
from .config import (
    SECURITY_MODES,
    ConfigError,
    Settings,
    load_dotenv,
    load_settings,
    read_env_text,
    unquote_env_value,
    unwrap_quoted_line,
    write_env_values,
)
from .mailbox import MailboxError, test_mailbox
from .masked_input import prompt_for_secret
from .sender import SmtpSendError

# No default. The endpoint differs per Qlar deployment, the CMS email panel prints the right one,
# and a plausible-looking guess is worse than a question: it fails at enrolment with a 404 that
# reads like a rejected code.
BASE_URL_HINT = "ends in /api/email-gateway - the CMS email panel shows it"

DEFAULT_PORTS = {
    ("imap", "ssl"): "993",
    ("imap", "starttls"): "143",
    ("smtp", "ssl"): "465",
    ("smtp", "starttls"): "587",
}

__all__ = [
    "SetupAborted",
    "ask_enrollment_code",
    "can_prompt",
    "check_connection",
    "run_setup",
]


class SetupAborted(Exception):
    """The operator cancelled the prompts, or there was no terminal to ask on."""


def can_prompt() -> bool:
    """True when there is a human at a console to answer.

    A container started without a TTY, a systemd unit and a cron job all answer False here and get
    the configuration error they have always got. Prompting into a log file that nobody is reading
    would turn a clear failure into a process that appears to hang.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - detached stdin
        return False


def run_setup(env_file: Path, *, ask_endpoint: bool = True, reason: str = "") -> tuple[Settings, bool]:
    """Asks for the mailbox details, saves them, and tests them.

    Returns the settings and whether the login test succeeded. A failed test is not fatal: the
    operator is told, offered another go, and — if they decline — left with a saved `.env` they
    can fix by hand or with `--init`. A mail server that is merely down at this moment is a normal
    thing for a service to survive.
    """
    try:
        load_dotenv(env_file)
    except ConfigError as error:
        print(f"Ignoring the existing file: {error}", file=sys.stderr)

    _banner(env_file, reason)

    while True:
        answers = _collect_answers(ask_endpoint=ask_endpoint)
        backup = write_env_values(env_file, answers)
        if backup is not None:
            print(f"\nThe previous {env_file} could not be read; it is kept as {backup}.")

        # The file is for the *next* start; this process is already past the point where it read
        # the environment, so the answers go into it directly.
        os.environ.update(answers)

        try:
            # Not `load_settings(env_file)`: real environment variables win there, which would
            # hide the answers just given.
            settings = load_settings(None)
        except ConfigError as error:
            print(f"That configuration cannot be used: {error}", file=sys.stderr)
            if _ask_yes_no("Enter the details again?", default=True):
                continue
            raise SetupAborted(str(error)) from error

        if check_connection(settings, prominent=True):
            return settings, True

        print()
        if _ask_yes_no("Enter the details again?", default=True):
            continue

        print(
            f"Leaving the details as saved in {env_file}. "
            "Fix them there, or run `qlar-email-gateway run --init` to be asked again.",
            file=sys.stderr,
        )
        return settings, False


def ask_enrollment_code() -> str:
    """Asks for the one-time code, rather than sending the operator back to edit a file.

    Not written to `.env`. The code is single-use and spent the moment enrolment succeeds, so
    keeping it would leave a dead credential on disk and one more thing to explain.
    """
    print()
    print("The one-time enrolment code is shown in the Qlar CMS:")
    print("  your agent > Channels > Email")
    print("It expires 15 minutes after it is generated, and works once.")

    while True:
        answer = _ask("  Enrolment code")
        # Forgiving about how it arrived: pasted with the quotes from a shell snippet, in lower
        # case, or with stray spaces. The alphabet the CMS generates is upper case.
        code = unquote_env_value(answer.strip()).strip().upper()
        if code:
            return code


def check_connection(settings: Settings, *, prominent: bool = False) -> bool:
    """Logs in to IMAP and SMTP once and reports what happened, in words.

    Used at every start, not only during setup: the most common support question about any
    on-premise agent is "is it actually talking to my mail server?", and the honest place to answer
    it is the first lines of the log rather than the first reply that never arrives.
    """
    mail = settings.mail
    address = (
        f"{mail.user}  imap {mail.imap_host}:{mail.imap_port} ({mail.imap_security})  "
        f"smtp {mail.smtp_host}:{mail.smtp_port} ({mail.smtp_security})"
    )

    if prominent:
        console.separator("TESTING THE MAILBOX")
        print(f"  {address}")
        print()

    # Run first, print second: a failure belongs entirely on stderr, and half a block on each
    # stream interleaves into nonsense the moment anyone redirects one of them.
    try:
        report = test_mailbox(settings)
    except (MailboxError, SmtpSendError) as error:
        server = "SMTP" if isinstance(error, SmtpSendError) else "IMAP"
        hint = _HINTS.get(error.category, "")
        lines = [f"{server}: {error}"] + ([f"hint: {hint}"] if hint else [])
        if prominent:
            console.badge("FAIL", "bad", error.category, *lines, file=sys.stderr)
        else:
            console.field("mailbox", address, file=sys.stderr)
            console.field("connection", f"FAILED ({error.category})", file=sys.stderr)
            for line in lines:
                console.detail(line, file=sys.stderr)
        return False

    idle = (
        "IDLE supported"
        if report.get("idleSupported")
        else f"no IDLE - checking every {settings.poll_interval_seconds}s"
    )
    summary = [
        f"IMAP: {_one_line(str(report.get('imapBanner') or ''))}",
        f"SMTP: {_one_line(str(report.get('smtpBanner') or ''))}",
        f"{idle}; {report.get('inboxCount', 0)} messages in {mail.imap_folder}",
    ]

    if prominent:
        console.badge(" OK ", "ok", "logged in to IMAP and SMTP", *summary)
    else:
        console.field("mailbox", address)
        console.success("connection", "OK")
        for line in summary:
            console.detail(line)
    return True


_HINTS = {
    "auth": "check MAIL_USER / MAIL_PASSWORD; Microsoft 365 and Google need an app password or SMTP AUTH",
    "connection": "check the host, port and IMAP_SECURITY / SMTP_SECURITY, and that a firewall allows them",
    "timeout": "the server did not answer; check the host and port, and any firewall in between",
}


def _banner(env_file: Path, reason: str = "") -> None:
    """The header, carrying only what the operator cannot already see.

    A fresh install gets none of it: they are standing in the directory, the file does not
    exist, and the reason they are being asked is that nothing is configured yet - three
    facts nobody needs told. The two lines that remain conditional earn their place only
    when they say something surprising: a file that already has settings in it (which is
    what makes "why is it asking me again?" answerable), or an env file somewhere other
    than here.
    """
    console.banner("Setup - a few answers before the gateway can start")

    if env_file.exists():
        console.field("found", _describe_existing(env_file))
    if env_file.resolve() != (Path.cwd() / ".env").resolve():
        console.field("saving to", env_file.resolve())
    if env_file.exists() or env_file.resolve() != (Path.cwd() / ".env").resolve():
        print()

    print("  Enter accepts the value in [brackets];  Press Ctrl-C to cancel.")
    print()


def _describe_existing(env_file: Path) -> str:
    """Says what was read from the file, by key name, before asking for it again.

    Being asked for something that is already written down is infuriating, and the reason
    is never visible: the file is in another directory, or the shell that wrote it used an
    encoding this cannot read, or the line is subtly not a setting. Naming the keys that
    were understood answers all three at a glance — and names only, never values, because
    one of them is a database password.
    """
    if not env_file.exists():
        return "no file there yet, so nothing is filled in for you"

    try:
        recognised = [
            line.partition("=")[0].strip()
            for raw in read_env_text(env_file).splitlines()
            if (line := unwrap_quoted_line(raw.strip())) and not line.startswith("#") and "=" in line
        ]
    except ConfigError as error:
        return f"unreadable - {error}"

    if not recognised:
        return "a file with no settings in it, so nothing is filled in for you"
    return ", ".join(recognised)


def _collect_answers(*, ask_endpoint: bool = True) -> dict[str, str]:
    """The questions themselves, in the order a mail provider's settings page lists them.

    `ask_endpoint` is False when the endpoint arrived on the command line: asking for an answer
    that was just supplied is how a tool teaches people to stop reading its prompts.
    """
    total = 11 if ask_endpoint else 10
    number = iter(f"{index}/{total}" for index in range(1, total + 1))

    console.heading(f"YOUR MAILBOX        ({total} answers in all)")
    print("    Incoming mail (IMAP), then outgoing mail (SMTP). The password stays on this machine.")
    print()

    imap_host = _ask("IMAP host", _current("IMAP_HOST") or None, next(number))
    imap_security_default = _current("IMAP_SECURITY").lower() or "ssl"
    imap_port_default = _current("IMAP_PORT") or DEFAULT_PORTS[("imap", imap_security_default)]
    imap_port = _ask_port("IMAP port", imap_port_default, next(number))
    imap_security = _ask_security("IMAP security", imap_security_default, next(number))

    smtp_host = _ask("SMTP host", _current("SMTP_HOST") or _guess_smtp_host(imap_host), next(number))
    smtp_security_default = _current("SMTP_SECURITY").lower() or "starttls"
    smtp_port_default = _current("SMTP_PORT") or DEFAULT_PORTS[("smtp", smtp_security_default)]
    smtp_port = _ask_port("SMTP port", smtp_port_default, next(number))
    smtp_security = _ask_security("SMTP security", smtp_security_default, next(number))

    user = _ask("Mailbox user", _current("MAIL_USER") or None, next(number))
    saved_password = _current("MAIL_PASSWORD", strip=False)
    password = _ask_secret(
        "Password", keep_label="unchanged" if saved_password else None, number=next(number)
    )
    password = password or saved_password

    address = _ask("Mail address", _current("MAIL_ADDRESS") or user, next(number))
    from_name = _ask_optional("Sender name", _current("MAIL_FROM_NAME"), next(number))

    if ask_endpoint:
        print()
        console.heading("QLAR")
        base_url = _ask("API endpoint", _current("QLAR_BASE_URL") or None, next(number)).rstrip("/")
    else:
        base_url = _current("QLAR_BASE_URL").rstrip("/")

    return {
        "QLAR_BASE_URL": base_url,
        "IMAP_HOST": imap_host,
        "IMAP_PORT": imap_port,
        "IMAP_SECURITY": imap_security,
        "SMTP_HOST": smtp_host,
        "SMTP_PORT": smtp_port,
        "SMTP_SECURITY": smtp_security,
        "MAIL_USER": user,
        "MAIL_PASSWORD": password,
        "MAIL_ADDRESS": address,
        "MAIL_FROM_NAME": from_name,
    }


def _guess_smtp_host(imap_host: str) -> str:
    """`imap.corp.test` -> `smtp.corp.test`; anything else is offered unchanged (one server)."""
    return "smtp." + imap_host[len("imap.") :] if imap_host.lower().startswith("imap.") else imap_host


def _current(name: str, *, strip: bool = True) -> str:
    value = os.environ.get(name, "")
    return value.strip() if strip else value


# The form's columns. Answers typed into a ragged prompt look like a ransom note, and
# the eye cannot check them against each other afterwards; one column for the label and
# one for the default puts every answer the operator typed in the same place.
LABEL_WIDTH = 14
DEFAULT_WIDTH = 16


def _prompt(label: str, default: str | None, number: str = "") -> str:
    """One line of the form: `  3/7 Port          [5432]        : `.

    A default too long for its column is printed above the question instead, and the
    bracket says `[keep]`. Letting it push the colon out of line would ruin the one thing
    the columns are for - every answer the operator types starting in the same place - and
    truncating it would hide the difference between a prod host and a dev one.
    """
    if default and len(default) + 2 > DEFAULT_WIDTH:
        print(f"  {'':<4}{console.paint('current'.ljust(LABEL_WIDTH), 'dim')}{default}")
        bracket = "[keep]"
    else:
        bracket = f"[{default}]" if default else ""

    return f"  {number:<4}{label.ljust(LABEL_WIDTH)}{bracket.ljust(DEFAULT_WIDTH)}: "


def _complain(message: str) -> None:
    """Says what was wrong with an answer, where the answer was, and marked as a problem."""
    print(f"  {' ' * 4}{console.paint('! ' + message, 'warn')}")


def _ask(label: str, default: str | None = None, number: str = "") -> str:
    while True:
        try:
            answer = input(_prompt(label, default, number)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SetupAborted("cancelled at the prompt") from None
        if answer:
            return answer
        if default:
            return default
        _complain("this one is required")


def _ask_secret(label: str, *, keep_label: str | None, number: str = "") -> str | None:
    """Reads a password, showing a mask per character. None means "keep what we had".

    Masked rather than invisible: this prompt is met once, by someone pasting a password
    into an unfamiliar tool, and a terminal that shows no reaction at all is
    indistinguishable from one that has stopped listening.
    """
    while True:
        try:
            answer = prompt_for_secret(_prompt(label, keep_label, number))
        except (EOFError, KeyboardInterrupt):
            print()
            raise SetupAborted("cancelled at the prompt") from None
        if answer:
            return answer
        if keep_label:
            return None
        _complain("a password is required (the gateway does not support passwordless login)")


def _ask_optional(label: str, default: str, number: str = "") -> str:
    """Like `_ask`, but Enter with nothing saved means "leave it blank"."""
    try:
        answer = input(_prompt(label, default or None, number)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SetupAborted("cancelled at the prompt") from None
    return answer or default


def _ask_port(label: str, default: str, number: str = "") -> str:
    while True:
        answer = _ask(label, default or None, number)
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return answer
        _complain("a port is a whole number between 1 and 65535")


def _ask_security(label: str, default: str, number: str = "") -> str:
    while True:
        answer = _ask(label, default, number).strip().lower()
        if answer in SECURITY_MODES:
            return answer
        _complain("ssl or starttls")


def _ask_yes_no(question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _one_line(text: str, limit: int = 100) -> str:
    """Server banners are multi-line and long; one readable line is the useful part."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."
