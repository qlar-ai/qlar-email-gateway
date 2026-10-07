"""Command line: `qlar-email-gateway <command>`.

Five commands, in the order an operator meets them:

    test-mailbox  log in to IMAP and SMTP before involving Qlar at all
    enroll        register with Qlar and serve — the only command most people need
    fingerprint   print this gateway's key fingerprint, to compare with the CMS
    run           serve, for a machine that is already enrolled
    version       what is installed, and which protocol it speaks

`enroll` is the whole installation. It enrols if this machine is not enrolled yet, prints the
fingerprint to compare, and then stays up — including through the wait for someone to click
Approve. Running it again on an enrolled machine skips enrolment and goes straight to serving,
which is what a container restart does. `run` remains for service definitions that would rather
not carry an enrolment step at all; it is the same thing.

Serving is two loops sharing one identity: the mailbox watcher (IMAP → Qlar) and the job loop
(Qlar → SMTP).

`run`, `test-mailbox` and `enroll` need mailbox settings. When those are missing and there is a
terminal to ask on, the setup prompts in `wizard.py` collect them and write `.env` instead of
printing a configuration error. Without a terminal — a container, a systemd unit — the
configuration error is printed on stderr, with the same exit code.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from . import PROTOCOL_VERSION, __version__, console
from .client import Revoked
from .config import (
    ConfigError,
    EnrollmentState,
    Settings,
    load_dotenv,
    load_settings,
    read_env_text,
    write_env_values,
)
from .crypto import fingerprint, load_or_create_private_key, public_key_pem
from .enroll import EnrollmentError, enroll
from .mailbox import MailboxWatcher
from .poll import PollLoop
from .status import MailboxStatus
from .wizard import SetupAborted, ask_enrollment_code, can_prompt, check_connection, run_setup

DEFAULT_ENV_FILE = ".env"

# The commands that need mailbox settings, and so may offer the setup prompts.
SETUP_COMMANDS = ("run", "test-mailbox", "enroll")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qlar-email-gateway",
        description="On-premise email gateway for Qlar. Outbound-only: no inbound port is opened.",
    )
    parser.add_argument(
        "--env-file", default=DEFAULT_ENV_FILE, help="path to the settings file (default: .env)"
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--version",
        action="version",
        version=f"qlar-email-gateway {__version__} (protocol {PROTOCOL_VERSION})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    _with_init_flag(
        subparsers.add_parser("run", help="watch the mailbox and send replies (already enrolled machines)")
    )
    enroll_parser = _with_init_flag(
        subparsers.add_parser(
            "enroll",
            help="register this gateway with Qlar and start serving (the command to use)",
        )
    )
    # The two values that come from Qlar rather than from this machine. As arguments they
    # travel in one copy-paste line, identical in every shell - neither contains a space, so
    # nothing needs quoting, which is exactly what went wrong when they travelled through
    # `echo KEY=value >> .env` instead.
    enroll_parser.add_argument(
        "--base-url",
        help="the Qlar API endpoint, as the CMS email panel prints it (saved to .env)",
    )
    enroll_parser.add_argument(
        "--code",
        help="the one-time enrolment code from the CMS (single use; not saved)",
    )
    _with_init_flag(
        subparsers.add_parser("test-mailbox", help="log in to IMAP and SMTP without contacting Qlar")
    )
    subparsers.add_parser("fingerprint", help="print this gateway's public key fingerprint")
    subparsers.add_parser("version", help="print version and protocol information")

    args = parser.parse_args(argv)
    console.make_output_safe()
    _configure_logging(args.log_level)

    if args.command == "version":
        print(f"qlar-email-gateway {__version__} (protocol {PROTOCOL_VERSION})")
        return 0

    settings, connection_ok = _settings_for(args)
    if settings is None:
        return 2

    if args.command == "test-mailbox":
        return _command_test_mailbox(settings, connection_ok)
    if args.command == "fingerprint":
        return _command_fingerprint(settings)
    if args.command == "enroll":
        return _command_enroll(settings, Path(args.env_file), getattr(args, "code", None), connection_ok)
    if args.command == "run":
        return _command_run(settings, connection_ok)

    return 2


def _with_init_flag(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--init",
        "-init",
        action="store_true",
        help="ask for the mailbox details again, even when .env is already filled in",
    )
    return parser


def _settings_for(args: argparse.Namespace) -> tuple[Settings | None, bool | None]:
    """Loads the settings, running the setup prompts when that is the helpful thing to do.

    Returns the settings and whether the mailbox login has already been tested:
    True or False when the prompts tested it, None when nothing has been checked yet. That
    third state is what stops `run` from opening two connections to say the same thing.
    """
    env_file = Path(args.env_file)

    # An endpoint given on the command line is the answer to a question the prompts would
    # otherwise ask, so it is applied before anything reads the environment.
    base_url = (getattr(args, "base_url", None) or "").strip().rstrip("/")
    if base_url:
        os.environ["QLAR_BASE_URL"] = base_url

    if getattr(args, "init", False):
        if not can_prompt():
            print(
                "--init needs a terminal to ask on. Edit .env directly, or run the container "
                "interactively (docker run -it ... run --init).",
                file=sys.stderr,
            )
            return None, None
        return _run_setup(env_file, ask_endpoint=not base_url)

    try:
        return load_settings(env_file), None
    except ConfigError as error:
        if args.command not in SETUP_COMMANDS or not can_prompt():
            print(f"configuration error: {error}", file=sys.stderr)
            return None, None

        # First run, most likely: no .env at all, or one that was never filled in. Ask
        # rather than explain, since everything the explanation would say is a question -
        # but carry the reason into the form, where it answers "why am I being asked?".
        return _run_setup(env_file, ask_endpoint=not base_url, reason=str(error))


def _run_setup(
    env_file: Path, *, ask_endpoint: bool = True, reason: str = ""
) -> tuple[Settings | None, bool | None]:
    try:
        return run_setup(env_file, ask_endpoint=ask_endpoint, reason=reason)
    except SetupAborted as error:
        print(f"setup cancelled: {error}", file=sys.stderr)
        return None, None


def _command_test_mailbox(settings: Settings, connection_ok: bool | None) -> int:
    # The check itself lives with the setup prompts: it is the same paragraph of output
    # there and here, and an operator comparing the two should not have to wonder whether
    # they mean the same thing.
    if connection_ok is None:
        connection_ok = check_connection(settings, prominent=True)
    return 0 if connection_ok else 1


def _command_fingerprint(settings: Settings) -> int:
    private_key, created = load_or_create_private_key(settings.key_file)
    if created:
        print(f"generated a new key pair at {settings.key_file}")
    print(fingerprint(public_key_pem(private_key)))
    return 0


def _command_enroll(
    settings: Settings, env_file: Path, code: str | None = None, connection_ok: bool | None = None
) -> int:
    """Makes sure this machine is enrolled, then serves. One command, start to finish.

    There used to be a second one. `enroll` printed a fingerprint and exited, and the operator
    was told to come back and type `run` after clicking Approve — a handover across a wait of
    unknown length, in a different window, often on a different day. People missed it, and the
    gateway that Qlar showed as approved was simply not running.

    So enrolment now flows straight into the poll loop, which has always tolerated not being
    approved yet and now says so in words. Approving in Qlar is the last thing anyone has to do.

    It is also idempotent, which is what makes it safe to be the only command: a machine that is
    already enrolled skips enrolment and goes straight to work. That is what a container restart
    does, and what an operator restarting a gateway that died should be able to do without
    hunting for a code that was single-use and is long gone.
    """
    if code:
        settings = replace(settings, enrollment_code=code.strip().strip("\"'").upper())

    # Whatever route the endpoint arrived by, later starts read it from the file.
    _remember_base_url(settings, env_file)

    state = EnrollmentState.load(settings.state_file)
    if state is not None and state.base_url == settings.base_url:
        # Already ours. Enrolling again is not just unnecessary, it is impossible: the code was
        # redeemed once and Qlar keeps only a hash of it.
        console.banner(f"Qlar Email Gateway {__version__}")
        console.field("endpoint", settings.base_url)
        console.field("gateway id", state.gateway_id)
        console.detail("already enrolled; starting")
        return _serve(settings, state, connection_ok)

    # The code was the one answer the setup prompts did not cover, so it had to be typed
    # into `.env` by hand - and the instructions for doing that are shell-specific in a way
    # that bites on Windows, where `echo 'KEY=value'` writes the quotes into the file. A
    # prompt has no shell in it.
    if settings.enrollment_code is None and can_prompt():
        try:
            settings = replace(settings, enrollment_code=ask_enrollment_code())
        except SetupAborted as error:
            print(f"cancelled: {error}", file=sys.stderr)
            return 2

    try:
        state, key_fingerprint = enroll(settings)
    except EnrollmentError as error:
        print(f"enrolment failed: {error}", file=sys.stderr)
        return 1

    console.banner("Enrolled. One thing left: approve this gateway in the Qlar CMS.")
    console.field("gateway id", state.gateway_id)
    console.field("state file", settings.state_file)
    print()
    console.heading("KEY FINGERPRINT -- compare with the one the CMS shows")
    print()
    console.fingerprint_block(key_fingerprint)
    print()
    console.step(1, "CMS -> your agent -> Channels -> Email")
    console.step(2, "check the fingerprint matches, then click Approve")
    console.step(3, "nothing. This keeps running and starts working when you do.")
    print()
    return _serve(settings, state, connection_ok)


def _remember_base_url(settings: Settings, env_file: Path) -> None:
    """Writes QLAR_BASE_URL to the env file when it is not already there.

    `--base-url` is given once, at enrolment, but every later `run` needs it. Leaving it in
    the environment of a process that is about to exit would mean the next start asks for
    an endpoint the operator has already supplied - which is the complaint this whole path
    exists to answer.
    """
    try:
        load_dotenv(env_file)
    except ConfigError:
        return  # unreadable file: the setup prompts deal with it, not this

    if os.environ.get("QLAR_BASE_URL") == settings.base_url and _env_file_has(env_file, "QLAR_BASE_URL"):
        return

    write_env_values(env_file, {"QLAR_BASE_URL": settings.base_url})


def _env_file_has(env_file: Path, key: str) -> bool:
    if not env_file.exists():
        return False
    try:
        return any(
            line.partition("=")[0].strip() == key
            for raw in read_env_text(env_file).splitlines()
            if (line := raw.strip()) and not line.startswith("#") and "=" in line
        )
    except ConfigError:
        return False


def _command_run(settings: Settings, connection_ok: bool | None) -> int:
    console.banner(f"Qlar Email Gateway {__version__}")
    console.field("endpoint", settings.base_url)

    state = EnrollmentState.load(settings.state_file)
    if state is None:
        print(
            f"not enrolled yet (no {settings.state_file}). Run: qlar-email-gateway enroll",
            file=sys.stderr,
        )
        return 2

    console.field("gateway id", state.gateway_id)

    if state.base_url != settings.base_url:
        print(
            f"QLAR_BASE_URL ({settings.base_url}) does not match the URL this gateway enrolled "
            f"against ({state.base_url}). Re-enrol if the Qlar endpoint really changed.",
            file=sys.stderr,
        )
        return 2

    return _serve(settings, state, connection_ok)


def _serve(settings: Settings, state: EnrollmentState, connection_ok: bool | None) -> int:
    """Checks the mailbox, then watches it and answers jobs until stopped or revoked.

    Shared by `run` and `enroll`, which differ only in how they got hold of the enrolment. The
    two loops share the signing key, the HTTP client, the state file, the send guard's memory and
    the mailbox status; either one learning that the gateway is revoked stops both.
    """
    if connection_ok is None:
        connection_ok = check_connection(settings)
    if not connection_ok:
        console.warning(
            "Starting anyway",
            "The mail server may simply be unreachable at this moment. The gateway keeps",
            "retrying and reports the mailbox state to Qlar on every poll.",
            "",
            "Run `qlar-email-gateway run --init` to re-enter the mailbox details.",
            file=sys.stderr,
        )

    print()
    print(f"  Watching {settings.mail.address} and polling for replies. Ctrl-C stops it.")
    print()

    log = logging.getLogger("qlar_email_gateway")
    status = MailboxStatus()
    loop = PollLoop(settings, state, status=status)
    watcher = MailboxWatcher(settings, state, loop.client, loop.audit, loop.guard, status)
    outcome = {"exit": 0}

    def watch() -> None:
        try:
            watcher.run_forever()
        except Revoked:
            log.error("this gateway has been revoked in the Qlar CMS; stopping")
            loop.stop()
        except Exception:
            # A bug, not a mail server problem (those are retried inside). Stop everything so a
            # supervisor restarts the process rather than leaving half a gateway running.
            log.exception("the mailbox watcher stopped unexpectedly")
            outcome["exit"] = 1
            loop.stop()

    thread = threading.Thread(target=watch, name="qlar-mailbox", daemon=True)

    def _handle_signal(signum, _frame):  # noqa: ANN001 - signal handler signature
        log.info("received signal %s, finishing in-flight jobs", signum)
        watcher.stop()
        loop.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    thread.start()
    loop.run_forever()
    watcher.stop()
    thread.join(timeout=10)
    return outcome["exit"]


def _configure_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # httpx logs every request at INFO, which put "POST .../enroll 200 OK" in the middle of
    # the one screen an operator reads carefully. Their own logs are wanted when someone is
    # debugging and noise otherwise, so they follow LOG_LEVEL down to DEBUG and no further.
    if resolved > logging.DEBUG:
        for library in ("httpx", "httpcore"):
            logging.getLogger(library).setLevel(logging.WARNING)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
