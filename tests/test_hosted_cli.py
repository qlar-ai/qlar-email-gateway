import pytest

from qlar_email_gateway.hosted import cli

ENV_KEYS = ("QLAR_BASE_URL", "QLAR_WORKER_API_KEY", "KEY_VAULT_URI", "STATE_DIR", "LOG_LEVEL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("missing", ["QLAR_BASE_URL", "QLAR_WORKER_API_KEY", "KEY_VAULT_URI"])
def test_missing_required_env_fails_fast_naming_the_key(monkeypatch, capsys, missing):
    values = {
        "QLAR_BASE_URL": "https://q/api/email-gateway",
        "QLAR_WORKER_API_KEY": "k",
        "KEY_VAULT_URI": "https://vault",
    }
    for key, value in values.items():
        if key != missing:
            monkeypatch.setenv(key, value)

    assert cli.main() == 1

    assert missing in capsys.readouterr().err


def test_plain_http_base_url_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("QLAR_BASE_URL", "http://q.example.com/api")
    monkeypatch.setenv("QLAR_WORKER_API_KEY", "k")
    monkeypatch.setenv("KEY_VAULT_URI", "https://vault")

    assert cli.main() == 1
    assert "https" in capsys.readouterr().err


class FakeController:
    def __init__(self, control, **kwargs):
        self.kwargs = kwargs
        self.stopped = False
        FakeController.last = self

    def run_forever(self):
        if FakeController.crash:
            raise RuntimeError("boom")
        self.stop_requested = True

    def stop(self):
        self.stopped = True


def configure_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QLAR_BASE_URL", "https://q/api/email-gateway")
    monkeypatch.setenv("QLAR_WORKER_API_KEY", "k")
    monkeypatch.setenv("KEY_VAULT_URI", "https://vault")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "HostedController", FakeController)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)


def test_main_runs_controller_and_exits_zero(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    FakeController.crash = False

    assert cli.main() == 0
    assert FakeController.last.kwargs["vault_uri"] == "https://vault"
    assert FakeController.last.kwargs["state_dir"] == tmp_path


def test_main_exits_one_on_crash(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    FakeController.crash = True

    assert cli.main() == 1


def test_signal_handlers_installed_once_and_stop_the_controller(monkeypatch, tmp_path):
    configure_env(monkeypatch, tmp_path)
    FakeController.crash = False
    installed = {}
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: installed.update({signum: handler}))

    cli.main()

    assert set(installed) == {cli.signal.SIGINT, cli.signal.SIGTERM}
    installed[cli.signal.SIGTERM](cli.signal.SIGTERM, None)
    assert FakeController.last.stopped
