"""The setup prompts, driven by a scripted console instead of a human.

Two things are being protected here. The first is the happy path an operator meets once: answer
the mailbox questions, get a working `.env`, see the login proved. The second is that automation
never meets it at all — a container has no terminal, and a gateway that stops to ask a question
nobody can see would look exactly like a gateway that has hung.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qlar_email_gateway import cli, wizard
from qlar_email_gateway.mailbox import MailboxError

ENDPOINT = "https://qlar.example.com/messenger/api/email-gateway"

MANAGED_KEYS = (
    "QLAR_BASE_URL",
    "QLAR_ENROLLMENT_CODE",
    "QLAR_GATEWAY_NAME",
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_SECURITY",
    "IMAP_FOLDER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURITY",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "MAIL_ADDRESS",
    "MAIL_FROM_NAME",
    "AUDIT_LOG_FILE",
)

#: Answers to the mailbox questions in order, accepting every default that exists.
MAILBOX_ANSWERS = (
    "imap.corp.test",  # IMAP host
    "",  # IMAP port [993]
    "",  # IMAP security [ssl]
    "",  # SMTP host [smtp.corp.test]
    "",  # SMTP port [587]
    "",  # SMTP security [starttls]
    "ask@corp.test",  # user
    "s3cret pass",  # password
    "",  # mailbox address [= user]
    "",  # sender name [blank]
)

COMPLETE_ENV = (
    f"QLAR_BASE_URL={ENDPOINT}",
    "IMAP_HOST=imap.corp.test",
    "SMTP_HOST=smtp.corp.test",
    "MAIL_USER=ask@corp.test",
    "MAIL_PASSWORD=secret",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Settings come from the environment, so every test needs an empty one."""
    for key in MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)


class Console:
    """A scripted terminal: answers are consumed in the order they were queued."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def input(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"nothing scripted for prompt {prompt!r}")
        return self.answers.pop(0)

    def install(self, monkeypatch, *, connection_ok: bool = True):
        monkeypatch.setattr("builtins.input", self.input)
        monkeypatch.setattr(wizard, "prompt_for_secret", self.input)
        monkeypatch.setattr(wizard, "can_prompt", lambda: True)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(connection_ok))
        return self


def _mailbox(ok: bool):
    def fake_test_mailbox(_settings):
        if ok:
            return {
                "imapBanner": "* OK Dovecot ready",
                "smtpBanner": "220 ready",
                "idleSupported": True,
                "inboxCount": 7,
            }
        raise MailboxError("auth", "IMAP login refused")

    return fake_test_mailbox


def env_values(env_file: Path) -> dict[str, str]:
    from qlar_email_gateway.config import unquote_env_value

    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = unquote_env_value(value.strip())
    return values


def write_env(env_file: Path, *lines: str) -> Path:
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


class TestFirstRun:
    def test_answers_become_a_working_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console(*MAILBOX_ANSWERS, ENDPOINT).install(monkeypatch)

        settings, ok = wizard.run_setup(env_file)

        assert ok is True
        mail = settings.mail
        assert (mail.imap_host, mail.imap_port, mail.imap_security) == ("imap.corp.test", 993, "ssl")
        assert (mail.smtp_host, mail.smtp_port, mail.smtp_security) == ("smtp.corp.test", 587, "starttls")
        assert (mail.user, mail.password, mail.address, mail.from_name) == (
            "ask@corp.test",
            "s3cret pass",
            "ask@corp.test",
            "",
        )

        saved = env_values(env_file)
        assert saved["IMAP_HOST"] == "imap.corp.test"
        assert saved["SMTP_HOST"] == "smtp.corp.test"
        assert saved["MAIL_PASSWORD"] == "s3cret pass"
        assert saved["QLAR_BASE_URL"] == ENDPOINT

    def test_smtp_host_is_offered_from_the_imap_host(self, tmp_path, monkeypatch):
        console = Console(*MAILBOX_ANSWERS, ENDPOINT).install(monkeypatch)

        wizard.run_setup(tmp_path / ".env")

        smtp_prompt = next(prompt for prompt in console.prompts if "SMTP host" in prompt)
        assert "[smtp.corp.test]" in smtp_prompt

    def test_a_different_address_and_sender_name(self, tmp_path, monkeypatch):
        answers = list(MAILBOX_ANSWERS)
        answers[8] = "Support@Corp.Test"
        answers[9] = "Corp Support"
        Console(*answers, ENDPOINT).install(monkeypatch)

        settings, _ = wizard.run_setup(tmp_path / ".env")

        assert settings.mail.address == "support@corp.test"
        assert settings.mail.from_name == "Corp Support"

    def test_a_security_answer_other_than_ssl_or_starttls_is_asked_again(self, tmp_path, monkeypatch):
        answers = list(MAILBOX_ANSWERS)
        answers[2:3] = ["tls", "starttls"]
        console = Console(*answers, ENDPOINT).install(monkeypatch)

        settings, _ = wizard.run_setup(tmp_path / ".env")

        assert settings.mail.imap_security == "starttls"
        assert console.answers == []


class TestAnsweringAgain:
    def test_saved_values_are_offered_as_defaults(self, tmp_path, monkeypatch):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        Console(
            "new-imap.corp.test",  # IMAP host changed
            "",
            "",
            "",
            "",
            "",  # the rest as saved / defaulted
            "",  # user, as saved
            "",  # password: Enter keeps the saved one
            "",
            "",
            "",  # endpoint, as saved
        ).install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.mail.imap_host == "new-imap.corp.test"
        assert settings.mail.smtp_host == "smtp.corp.test", "a saved SMTP host is not replaced by a guess"
        assert settings.mail.password == "secret"

    def test_a_failed_login_offers_another_attempt(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        # First round fails; "y"; second round accepts every saved answer (10 mailbox + endpoint),
        # fails again; "n".
        console = Console(*MAILBOX_ANSWERS, ENDPOINT, "y", *([""] * 11), "n").install(
            monkeypatch, connection_ok=False
        )

        settings, ok = wizard.run_setup(env_file)

        assert ok is False
        assert console.answers == []
        assert sum("Enter the details again" in prompt for prompt in console.prompts) == 2
        assert env_values(env_file)["IMAP_HOST"] == "imap.corp.test"

    def test_declining_another_attempt_keeps_the_saved_answers(self, tmp_path, monkeypatch, capsys):
        env_file = tmp_path / ".env"
        Console(*MAILBOX_ANSWERS, ENDPOINT, "n").install(monkeypatch, connection_ok=False)

        settings, ok = wizard.run_setup(env_file)

        assert ok is False
        assert settings.mail.imap_host == "imap.corp.test"
        assert "--init" in capsys.readouterr().err


class TestTheQlarEndpoint:
    def test_it_is_required_rather_than_defaulted(self, tmp_path, monkeypatch):
        console = Console(*MAILBOX_ANSWERS, "", ENDPOINT).install(monkeypatch)

        settings, _ = wizard.run_setup(tmp_path / ".env")

        assert settings.base_url == ENDPOINT
        assert console.answers == []

    def test_a_trailing_slash_is_not_carried_into_signed_paths(self, tmp_path, monkeypatch):
        Console(*MAILBOX_ANSWERS, ENDPOINT + "/").install(monkeypatch)

        settings, _ = wizard.run_setup(tmp_path / ".env")

        assert settings.base_url == ENDPOINT


class TestWithoutATerminal:
    def test_missing_configuration_still_fails_the_old_way(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(wizard, "can_prompt", lambda: False)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli.main(["--env-file", str(tmp_path / ".env"), "test-mailbox"])

        assert exit_code == 2
        assert "configuration error" in capsys.readouterr().err

    def test_init_says_why_it_cannot_ask(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli.main(["--env-file", str(tmp_path / ".env"), "run", "--init"])

        assert exit_code == 2
        assert "needs a terminal" in capsys.readouterr().err


class TestTheInitFlag:
    @pytest.mark.parametrize("flag", ["--init", "-init"])
    def test_both_spellings_re_ask_over_a_complete_env_file(self, tmp_path, monkeypatch, flag):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        Console("new-imap.corp.test", "", "", "", "", "", "", "", "", "", "").install(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "test-mailbox", flag])

        assert env_values(env_file)["IMAP_HOST"] == "new-imap.corp.test"

    def test_without_the_flag_a_complete_env_file_asks_nothing(self, tmp_path, monkeypatch, capsys):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("a complete .env must not trigger the prompts")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(True))

        assert cli.main(["--env-file", str(env_file), "test-mailbox"]) == 0
        out = capsys.readouterr().out
        assert "Dovecot" in out
        assert "IDLE" in out


class TestTestMailbox:
    def test_a_failed_login_exits_non_zero_and_names_the_category(self, tmp_path, monkeypatch, capsys):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(False))

        assert cli.main(["--env-file", str(env_file), "test-mailbox"]) == 1
        assert "auth" in capsys.readouterr().err

    def test_the_password_is_never_printed(self, tmp_path, monkeypatch, capsys):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(True))

        cli.main(["--env-file", str(env_file), "test-mailbox"])

        captured = capsys.readouterr()
        assert "secret" not in captured.out + captured.err


class TestTheEnrolmentCode:
    def _capture(self, monkeypatch) -> dict:
        seen: dict = {}

        def fake_enroll(settings):
            seen["base_url"] = settings.base_url
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stopping here; the code is what this test is about")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        return seen

    def test_it_is_asked_for_when_missing(self, tmp_path, monkeypatch):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        Console("3gsl-d4gd-v7yy").install(monkeypatch)
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll"])

        assert seen["code"] == "3GSL-D4GD-V7YY"

    def test_quotes_from_a_pasted_shell_snippet_are_stripped(self, tmp_path, monkeypatch):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        Console("'3GSL-D4GD-V7YY'").install(monkeypatch)
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll"])

        assert seen["code"] == "3GSL-D4GD-V7YY"

    def test_without_a_terminal_the_old_error_stands(self, tmp_path, monkeypatch, capsys):
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(True))

        assert cli.main(["--env-file", str(env_file), "enroll"]) == 1
        assert "QLAR_ENROLLMENT_CODE is not set" in capsys.readouterr().err


class TestEnrollingInOneLine:
    def _mailbox_only(self, tmp_path) -> Path:
        return write_env(tmp_path / ".env", *COMPLETE_ENV[1:])

    def _capture(self, monkeypatch) -> dict:
        seen: dict = {}

        def fake_enroll(settings):
            seen["base_url"] = settings.base_url
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stopping here; the arguments are what this tests")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(True))
        return seen

    def test_both_values_reach_enrolment_without_a_prompt(self, tmp_path, monkeypatch):
        env_file = self._mailbox_only(tmp_path)
        seen = self._capture(monkeypatch)

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("nothing should be asked when both values were given")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", ENDPOINT, "--code", "86lp-6z8g-xw4q"])

        assert seen["base_url"] == ENDPOINT
        assert seen["code"] == "86LP-6Z8G-XW4Q"

    def test_the_endpoint_is_saved_and_the_code_is_not(self, tmp_path, monkeypatch):
        env_file = self._mailbox_only(tmp_path)
        self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", ENDPOINT, "--code", "SECRET-CODE"])

        text = env_file.read_text(encoding="utf-8")
        assert f"QLAR_BASE_URL={ENDPOINT}" in text
        assert "SECRET-CODE" not in text

    def test_the_mailbox_is_still_asked_for_when_it_is_unknown(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        seen = self._capture(monkeypatch)
        console = Console(*MAILBOX_ANSWERS).install(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", ENDPOINT, "--code", "X"])

        assert seen["base_url"] == ENDPOINT
        assert console.answers == []
        assert not any("endpoint" in prompt.lower() for prompt in console.prompts)


class TestReadOnlyContainers:
    """`docker-compose.example.yml` runs with a read-only root filesystem and the settings in the
    real environment; enrolment must not try (and fail) to write them into a `.env` there."""

    def _capture(self, monkeypatch) -> dict:
        seen: dict = {}

        def fake_enroll(settings):
            seen["base_url"] = settings.base_url
            raise cli.EnrollmentError("stopping here")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        monkeypatch.setattr(wizard, "test_mailbox", _mailbox(True))
        monkeypatch.setattr(cli, "can_prompt", lambda: False)
        return seen

    def test_an_endpoint_from_the_environment_is_not_written(self, tmp_path, monkeypatch):
        seen = self._capture(monkeypatch)
        for line in COMPLETE_ENV:
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, value)
        env_file = tmp_path / ".env"

        cli.main(["--env-file", str(env_file), "enroll", "--code", "X"])

        assert seen["base_url"] == ENDPOINT
        assert not env_file.exists()

    def test_an_unwritable_env_file_is_a_warning_not_a_crash(self, tmp_path, monkeypatch, capsys):
        seen = self._capture(monkeypatch)
        env_file = write_env(tmp_path / ".env", *COMPLETE_ENV[1:])

        def read_only(*_args, **_kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(cli, "write_env_values", read_only)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", ENDPOINT, "--code", "X"])

        assert seen["base_url"] == ENDPOINT
        assert "could not save QLAR_BASE_URL" in capsys.readouterr().err
