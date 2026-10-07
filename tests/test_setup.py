"""The setup prompts, driven by a scripted console instead of a human.

Two things are being protected here. The first is the happy path an operator meets once:
answer six questions, get a working `.env`, see the connection proved. The second is that
automation never meets it at all — a container has no terminal, and a gateway that stops
to ask a question nobody can see would look exactly like a gateway that has hung.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qlar_email_gateway import cli, wizard
from qlar_email_gateway.executor import ExecutionResult

ENDPOINT = "https://qlar.example.com/api/email-gateway"

MANAGED_KEYS = (
    "QLAR_BASE_URL",
    "QLAR_ENROLLMENT_CODE",
    "QLAR_GATEWAY_NAME",
    "DB_PROVIDER",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "TABLE_ALLOWLIST",
    "AUDIT_LOG_FILE",
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

    def install(self, monkeypatch, *, connection_ok: bool = True, writable: bool | None = False):
        monkeypatch.setattr("builtins.input", self.input)
        monkeypatch.setattr(wizard, "prompt_for_secret", self.input)
        monkeypatch.setattr(wizard, "can_prompt", lambda: True)
        monkeypatch.setattr(wizard, "test_connection", _connection(connection_ok))
        monkeypatch.setattr(wizard, "account_can_write", lambda _settings: writable)
        return self


def _connection(ok: bool):
    def fake_test_connection(_settings):
        if ok:
            return ExecutionResult(status="ok", rows=[["PostgreSQL 16.4"]], row_count=1, duration_ms=7)
        return ExecutionResult(
            status="error",
            duration_ms=3,
            error={"category": "connection", "messageText": "could not connect", "hint": None},
        )

    return fake_test_connection


def env_values(env_file: Path) -> dict[str, str]:
    from qlar_email_gateway.config import unquote_env_value

    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = unquote_env_value(value.strip())
    return values


class TestFirstRun:
    def test_answers_become_a_working_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console(
            "",  # database type: the offered default
            "db.internal",  # host
            "",  # port: the provider's default
            "warehouse",  # database name
            "qlar_readonly",  # username
            "s3cret pass",  # password, masked at the prompt
            ENDPOINT,  # Qlar endpoint
        ).install(monkeypatch)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is True
        assert settings.database.provider == "postgresql"
        assert settings.database.port == 5432
        assert settings.database.password == "s3cret pass"
        assert settings.base_url == ENDPOINT

        saved = env_values(env_file)
        assert saved["DB_HOST"] == "db.internal"
        assert saved["DB_PORT"] == "5432"
        assert saved["DB_NAME"] == "warehouse"
        assert saved["DB_USER"] == "qlar_readonly"
        assert saved["DB_PASSWORD"] == "s3cret pass"

    def test_a_pasted_url_fills_in_the_rest(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        console = Console(
            "mysql",
            "mysql://ana:pw%40word@db.internal:3307/warehouse",
            "",  # port, offered as 3307 from the URL
            "",  # database name, offered as warehouse
            "",  # username, offered as ana
            "",  # password: keep the one from the URL
            ENDPOINT,  # Qlar endpoint
        ).install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.provider == "mysql"
        assert settings.database.host == "db.internal"
        assert settings.database.port == 3307
        assert settings.database.database == "warehouse"
        assert settings.database.user == "ana"
        assert settings.database.password == "pw@word"
        # The parsed values are shown as defaults, not applied silently.
        assert any("3307" in prompt for prompt in console.prompts)

    def test_a_bare_host_and_port_is_understood_too(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console("1", "db.internal:6432", "", "warehouse", "reader", "pw", ENDPOINT).install(
            monkeypatch
        )

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.host == "db.internal"
        assert settings.database.port == 6432


class TestAnsweringAgain:
    def test_saved_values_are_offered_as_defaults(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "# hand-written, keep me",
                    "QLAR_BASE_URL=https://qlar.example.com/api/email-gateway",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=old.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=old-secret",
                    "MAX_CONCURRENT_QUERIES=9",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # Every answer is Enter except the host: the one field being changed.
        Console("", "new.internal", "", "", "", "", "").install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.host == "new.internal"
        assert settings.database.password == "old-secret"
        assert settings.base_url == "https://qlar.example.com/api/email-gateway"

        text = env_file.read_text(encoding="utf-8")
        assert "# hand-written, keep me" in text
        assert "MAX_CONCURRENT_QUERIES=9" in text

    def test_a_failed_connection_offers_another_attempt(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        console = Console(
            "", "db.internal", "", "warehouse", "reader", "pw", ENDPOINT,
            "n",  # no, do not enter them again
        ).install(monkeypatch, connection_ok=False)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is False
        # The answers are still saved: they are usually nearly right, and an operator who
        # fixes one line by hand should not have to retype the other six.
        assert env_values(env_file)["DB_HOST"] == "db.internal"
        assert settings.database.host == "db.internal"
        assert console.answers == []

    def test_declining_at_the_prompt_can_be_retried(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console(
            "", "typo.internal", "", "warehouse", "reader", "pw", ENDPOINT,
            "y",  # yes, ask again
            "", "db.internal", "", "warehouse", "reader", "pw", "",
            "n",
        ).install(monkeypatch, connection_ok=False)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is False
        assert settings.database.host == "db.internal"


class TestTheQlarEndpoint:
    """The address of Qlar itself, which is the one answer nobody can guess for you.

    It differs per deployment, and a plausible default is worse than a question: pointed at
    a Qlar *web* address instead of its API, enrolment fails with a 404 that reads exactly
    like a rejected enrolment code, and the operator spends the afternoon generating fresh
    codes that fail the same way.
    """

    def test_it_is_required_rather_than_defaulted(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        console = Console(
            "", "db.internal", "", "warehouse", "reader", "pw",
            "",         # Enter: there is nothing to fall back to, so it asks again
            ENDPOINT,
        ).install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.base_url == ENDPOINT
        assert console.answers == []

    def test_a_trailing_slash_is_not_carried_into_signed_paths(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console("", "db.internal", "", "warehouse", "reader", "pw", ENDPOINT + "/").install(
            monkeypatch
        )

        settings, _ = wizard.run_setup(env_file)

        assert settings.base_url == ENDPOINT


class TestWithoutATerminal:
    def test_missing_configuration_still_fails_the_old_way(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(wizard, "can_prompt", lambda: False)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli.main(["--env-file", str(tmp_path / ".env"), "test-db"])

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
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "QLAR_BASE_URL=https://qlar.example.com/api/email-gateway",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=old.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=old-secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        Console("", "new.internal", "", "", "", "", "").install(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        exit_code = cli.main(["--env-file", str(env_file), "test-db", flag])

        assert exit_code == 0
        assert env_values(env_file)["DB_HOST"] == "new.internal"

    def test_without_the_flag_a_complete_env_file_asks_nothing(self, tmp_path, monkeypatch, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "QLAR_BASE_URL=https://qlar.example.com/api/email-gateway",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=db.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("a filled-in .env must not be questioned")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(wizard, "test_connection", _connection(True))
        monkeypatch.setattr(wizard, "account_can_write", lambda _settings: False)

        assert cli.main(["--env-file", str(env_file), "test-db"]) == 0
        assert "connected in 7 ms" in capsys.readouterr().out


class TestTheEnrolmentCode:
    """The one answer the prompts used to leave out, and the one most often mistyped.

    It travels from a web page, through a clipboard, into a file, via whatever shell the
    operator happens to have — and on cmd.exe `echo 'KEY=value' >> .env` writes the quotes
    into the file, so the gateway reports the code as unset while it is visibly there.
    Asking for it removes every step in that chain except the clipboard.
    """

    def _configured(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    f"QLAR_BASE_URL={ENDPOINT}",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=db.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return env_file

    def test_it_is_asked_for_when_missing(self, tmp_path, monkeypatch):
        env_file = self._configured(tmp_path)
        Console("3gsl-d4gd-v7yy").install(monkeypatch)

        seen = {}

        def fake_enroll(settings):
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stopping here; the code is what this test is about")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll"])

        # Upper-cased on the way through: the CMS generates from an upper-case alphabet,
        # and a lower-case paste would otherwise fail the hash comparison server-side.
        assert seen["code"] == "3GSL-D4GD-V7YY"

    def test_quotes_from_a_pasted_shell_snippet_are_stripped(self, tmp_path, monkeypatch):
        env_file = self._configured(tmp_path)
        Console("'3GSL-D4GD-V7YY'").install(monkeypatch)

        seen = {}

        def fake_enroll(settings):
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stop")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll"])

        assert seen["code"] == "3GSL-D4GD-V7YY"

    def test_a_configured_code_is_not_questioned(self, tmp_path, monkeypatch):
        env_file = self._configured(tmp_path)
        with env_file.open("a", encoding="utf-8") as handle:
            handle.write("QLAR_ENROLLMENT_CODE=ALREADY-SET-HERE\n")

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("a code that is already configured must not be asked for")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        seen = {}

        def fake_enroll(settings):
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stop")

        monkeypatch.setattr(cli, "enroll", fake_enroll)

        cli.main(["--env-file", str(env_file), "enroll"])

        assert seen["code"] == "ALREADY-SET-HERE"

    def test_without_a_terminal_the_old_error_stands(self, tmp_path, monkeypatch, capsys):
        env_file = self._configured(tmp_path)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        assert cli.main(["--env-file", str(env_file), "enroll"]) == 1
        assert "QLAR_ENROLLMENT_CODE is not set" in capsys.readouterr().err


class TestEnrollingInOneLine:
    """`enroll --base-url ... --code ...`: the two values Qlar knows, as arguments.

    They have to cross from a browser to a terminal somehow. Through `.env` they cross a
    shell, which quotes and encodes differently on every platform and broke two installs.
    Through a prompt they cross a clipboard twice. As arguments they are one copy-paste
    line that reads the same in bash, cmd and PowerShell — neither value contains a space,
    so there is nothing to quote.
    """

    ENDPOINT = "https://qlar.example.com/api/email-gateway"

    def _database_only(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=db.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return env_file

    def _capture(self, monkeypatch):
        seen = {}

        def fake_enroll(settings):
            seen["base_url"] = settings.base_url
            seen["code"] = settings.enrollment_code
            raise cli.EnrollmentError("stopping here; the arguments are what this tests")

        monkeypatch.setattr(cli, "enroll", fake_enroll)
        return seen

    def test_both_values_reach_enrolment_without_a_prompt(self, tmp_path, monkeypatch):
        env_file = self._database_only(tmp_path)
        seen = self._capture(monkeypatch)

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("nothing should be asked when both values were given")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(
            [
                "--env-file", str(env_file), "enroll",
                "--base-url", self.ENDPOINT,
                "--code", "86lp-6z8g-xw4q",
            ]
        )

        assert seen["base_url"] == self.ENDPOINT
        assert seen["code"] == "86LP-6Z8G-XW4Q", "upper-cased, as the CMS generates it"

    def test_a_trailing_slash_does_not_reach_the_signed_paths(self, tmp_path, monkeypatch):
        env_file = self._database_only(tmp_path)
        seen = self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(
            ["--env-file", str(env_file), "enroll", "--base-url", self.ENDPOINT + "/", "--code", "X"]
        )

        assert seen["base_url"] == self.ENDPOINT

    def test_the_endpoint_is_saved_so_the_next_run_does_not_ask(self, tmp_path, monkeypatch):
        env_file = self._database_only(tmp_path)
        self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", self.ENDPOINT, "--code", "X"])

        assert f"QLAR_BASE_URL={self.ENDPOINT}" in env_file.read_text(encoding="utf-8")

    def test_the_code_is_not_saved(self, tmp_path, monkeypatch):
        # Single use, and spent the moment enrolment succeeds. Keeping it would leave a dead
        # credential in a file that also holds the database password.
        env_file = self._database_only(tmp_path)
        self._capture(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(
            ["--env-file", str(env_file), "enroll", "--base-url", self.ENDPOINT, "--code", "SECRET-CODE"]
        )

        assert "SECRET-CODE" not in env_file.read_text(encoding="utf-8")

    def test_the_database_is_still_asked_for_when_it_is_unknown(self, tmp_path, monkeypatch):
        # The endpoint and the code come from Qlar; the database does not, and nothing on
        # this path can know it.
        env_file = tmp_path / ".env"
        seen = self._capture(monkeypatch)
        console = Console("", "db.internal", "", "warehouse", "reader", "pw").install(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        cli.main(["--env-file", str(env_file), "enroll", "--base-url", self.ENDPOINT, "--code", "X"])

        assert seen["base_url"] == self.ENDPOINT
        # Every scripted answer was used, and none of them was an endpoint: the Qlar
        # question is skipped when the answer arrived on the command line.
        assert console.answers == []
        assert not any("endpoint" in prompt.lower() for prompt in console.prompts)
