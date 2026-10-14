import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from qlar_email_gateway.config import MailSettings
from qlar_email_gateway.hosted.api import (
    ControlPlaneAuthError,
    ControlPlaneRejected,
    ControlPlaneUnreachable,
)
from qlar_email_gateway.hosted.controller import MAX_MAILBOXES, HostedController
from qlar_email_gateway.hosted.credentials import CredentialFormatError
from qlar_email_gateway.mailbox import MailboxError

BASE = "https://q/api/email-gateway"


def row(gateway_id, status="active", enrolled=True, code=None):
    return {
        "gatewayId": gateway_id,
        "status": status,
        "mailboxAddress": f"{gateway_id}@x.com",
        "secretName": f"secret-{gateway_id}",
        "enrolled": enrolled,
        "enrollmentCode": code,
    }


class FakeControl:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.results: list[tuple[str, dict]] = []
        self.list_error: Exception | None = None
        self.poll_error: Exception | None = None

    def list_registrations(self):
        if self.list_error:
            raise self.list_error
        return list(self.rows)

    def poll(self):
        if self.poll_error:
            raise self.poll_error
        return None

    def post_result(self, control_id, payload):
        self.results.append((control_id, payload))


class FakeRuntime:
    created: list["FakeRuntime"] = []

    def __init__(self, registration, mail, base_url, state_dir, control, executor):
        self.registration, self.mail = registration, mail
        self.gateway_id = registration["gatewayId"]
        self.alive = True
        self.stopped_by: str | None = None
        self.events: list[str] = []
        FakeRuntime.created.append(self)

    def ensure_enrolled(self):
        self.events.append("enrolled")

    def start(self):
        self.events.append("started")

    def request_stop(self):
        self.events.append("request_stop")

    def join(self, timeout):
        self.events.append("join")
        self.alive = False

    def drop_credentials(self):
        self.mail = None

    def stop(self):
        self.request_stop()
        self.join(10)
        self.drop_credentials()
        self.events.append("stopped")

    @property
    def is_alive(self):
        return self.alive


def fake_fetch(vault_uri, secret_name):
    return MailSettings(
        imap_host="i", smtp_host="s", user="u", password="p", address=f"{secret_name}@x.com"
    )


def make_controller(control, tmp_path, fetch=fake_fetch, **kwargs):
    FakeRuntime.created = []
    return HostedController(
        control,
        vault_uri="https://vault",
        base_url=BASE,
        state_dir=tmp_path,
        fetch_credentials=fetch,
        runtime_factory=FakeRuntime,
        **kwargs,
    )


def running(controller):
    return {r.gateway_id for r in FakeRuntime.created if r.alive}


def test_reconcile_starts_missing_runtimes(tmp_path):
    controller = make_controller(FakeControl([row("a"), row("b")]), tmp_path)

    controller.reconcile_once()
    controller.reconcile_once()

    assert running(controller) == {"a", "b"}
    assert len(FakeRuntime.created) == 2
    assert FakeRuntime.created[0].events == ["enrolled", "started"]


def test_reconcile_stops_runtime_when_registration_disappears(tmp_path):
    control = FakeControl([row("a"), row("b")])
    controller = make_controller(control, tmp_path)
    controller.reconcile_once()

    control.rows = [row("a")]
    controller.reconcile_once()

    assert running(controller) == {"a"}
    stopped = next(r for r in FakeRuntime.created if r.gateway_id == "b")
    assert stopped.events[-1] == "stopped" and stopped.mail is None


def test_revoked_registration_is_stopped_and_not_restarted(tmp_path):
    control = FakeControl([row("a")])
    controller = make_controller(control, tmp_path)
    controller.reconcile_once()

    control.rows = [row("a", status="revoked")]
    controller.reconcile_once()
    controller.reconcile_once()

    assert running(controller) == set()
    assert len(FakeRuntime.created) == 1


def test_runtime_stopped_by_deletion_is_dropped_and_not_reenrolled_while_listed(tmp_path):
    control = FakeControl([row("a")])
    controller = make_controller(control, tmp_path)
    controller.reconcile_once()
    runtime = FakeRuntime.created[0]
    runtime.alive, runtime.stopped_by = False, "deleted"

    controller.reconcile_once()
    controller.reconcile_once()

    assert len(FakeRuntime.created) == 1
    assert runtime.mail is None  # credentials dropped
    assert "a" not in controller.runtimes

    # Once the server stops listing it, and later lists it again, it may start afresh.
    control.rows = []
    controller.reconcile_once()
    control.rows = [row("a")]
    controller.reconcile_once()
    assert len(FakeRuntime.created) == 2


def test_runtime_that_died_without_a_stop_reason_is_restarted(tmp_path):
    controller = make_controller(FakeControl([row("a")]), tmp_path)
    controller.reconcile_once()
    FakeRuntime.created[0].alive = False

    controller.reconcile_once()

    assert len(FakeRuntime.created) == 2 and FakeRuntime.created[1].alive


def test_cap_refuses_excess_with_one_error_log(tmp_path, caplog):
    rows = [row(f"g{i:03d}") for i in range(MAX_MAILBOXES + 5)]
    controller = make_controller(FakeControl(rows), tmp_path)

    with caplog.at_level(logging.ERROR):
        controller.reconcile_once()

    assert len(running(controller)) == MAX_MAILBOXES
    assert "g200" not in running(controller)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "5" in errors[0].getMessage()


def test_max_mailboxes_is_200():
    assert MAX_MAILBOXES == 200


def test_credential_error_skips_that_mailbox_only(tmp_path, caplog):
    def fetch(vault_uri, secret_name):
        if secret_name == "secret-bad":
            raise CredentialFormatError("the credential secret is missing: smtpHost")
        if secret_name == "secret-gone":
            raise KeyError("SecretNotFound")
        return fake_fetch(vault_uri, secret_name)

    controller = make_controller(
        FakeControl([row("bad"), row("gone"), row("ok")]), tmp_path, fetch=fetch
    )

    with caplog.at_level(logging.ERROR):
        controller.reconcile_once()

    assert running(controller) == {"ok"}
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert any("bad" in m and "smtpHost" in m for m in errors)


def test_enrolment_failure_skips_that_mailbox_only(tmp_path, caplog):
    class FailingRuntime(FakeRuntime):
        def ensure_enrolled(self):
            if self.gateway_id == "bad":
                raise RuntimeError("boom")
            super().ensure_enrolled()

    controller = make_controller(FakeControl([row("bad"), row("ok")]), tmp_path)
    controller._runtime_factory = FailingRuntime

    controller.reconcile_once()

    assert "ok" in controller.runtimes and "bad" not in controller.runtimes


def test_remove_command_stops_and_drops(tmp_path):
    controller = make_controller(FakeControl([row("a")]), tmp_path)
    controller.reconcile_once()

    controller.handle_command({"id": "c1", "type": "remove", "gatewayId": "a"})

    assert running(controller) == set() and "a" not in controller.runtimes


@pytest.mark.parametrize("kind", ["configure", "refresh_credentials"])
def test_configure_and_refresh_restart_with_fresh_credentials(tmp_path, kind):
    control = FakeControl([row("a")])
    controller = make_controller(control, tmp_path)
    controller.reconcile_once()

    controller.handle_command({"id": "c1", "type": kind, "gatewayId": "a"})

    assert FakeRuntime.created[0].events[-1] == "stopped"
    assert len(FakeRuntime.created) == 2 and FakeRuntime.created[1].alive
    assert set(controller.runtimes) == {"a"}


def test_configure_for_unknown_registration_is_ignored(tmp_path):
    controller = make_controller(FakeControl([]), tmp_path)

    controller.handle_command({"id": "c1", "type": "configure", "gatewayId": "zzz"})

    assert FakeRuntime.created == []


GOOD_CREDENTIALS = {
    "imapHost": "imap.x",
    "imapPort": 993,
    "imapSecurity": "ssl",
    "smtpHost": "smtp.x",
    "smtpPort": 587,
    "smtpSecurity": "starttls",
    "username": "u",
    "password": "p",
    "address": "a@x.com",
}


def run_test_credentials(tmp_path, credentials, tester):
    control = FakeControl()
    controller = make_controller(control, tmp_path, mailbox_tester=tester)
    controller.handle_command({"id": "ctl_1", "type": "test_credentials", "credentials": credentials})
    controller._probe_executor.shutdown(wait=True)
    return control.results


def test_test_credentials_posts_ok_result(tmp_path):
    seen = {}

    def tester(settings):
        seen["address"] = settings.mail.address
        return {"imapBanner": "hi", "smtpBanner": "yo", "idleSupported": True, "inboxCount": 3}

    results = run_test_credentials(tmp_path, GOOD_CREDENTIALS, tester)

    assert seen["address"] == "a@x.com"
    assert len(results) == 1 and results[0][0] == "ctl_1"
    payload = results[0][1]
    assert payload["status"] == "ok" and payload["idleSupported"] is True
    assert payload["imapBanner"] == "hi" and "durationMs" in payload


def test_test_credentials_posts_error_result_on_login_failure(tmp_path):
    def tester(settings):
        raise MailboxError("auth", "IMAP login refused")

    results = run_test_credentials(tmp_path, GOOD_CREDENTIALS, tester)

    payload = results[0][1]
    assert payload["status"] == "error"
    assert payload["error"]["category"] == "auth"
    assert "p" != payload["error"]["messageText"]


def test_test_credentials_posts_error_result_for_malformed_credentials(tmp_path):
    results = run_test_credentials(tmp_path, {"imapHost": "x"}, lambda settings: {})

    payload = results[0][1]
    assert payload["status"] == "error" and "smtpHost" in payload["error"]["messageText"]


def test_test_credentials_posts_error_even_on_unexpected_failure(tmp_path):
    def tester(settings):
        raise RuntimeError("boom")

    results = run_test_credentials(tmp_path, GOOD_CREDENTIALS, tester)

    assert results[0][1]["status"] == "error"


# -- the loops' error handling ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ControlPlaneUnreachable("down"),
        ControlPlaneRejected(404, "nope"),
        RuntimeError("bug"),
    ],
)
def test_attempt_backs_off_instead_of_crashing(tmp_path, error):
    control = FakeControl()
    control.list_error = error
    controller = make_controller(control, tmp_path)

    assert controller._attempt("reconcile", controller.reconcile_once) is False
    assert controller.fatal is None


def test_attempt_success_returns_true(tmp_path):
    controller = make_controller(FakeControl(), tmp_path)

    assert controller._attempt("reconcile", controller.reconcile_once) is True


def test_auth_error_is_fatal_and_stops_the_controller(tmp_path):
    control = FakeControl()
    control.list_error = ControlPlaneAuthError("refused")
    controller = make_controller(control, tmp_path)

    controller._attempt("reconcile", controller.reconcile_once)

    assert isinstance(controller.fatal, ControlPlaneAuthError)
    assert controller._stopped.is_set()


def test_run_forever_reconciles_immediately_and_stops_runtimes_on_stop(tmp_path):
    control = FakeControl([row("a")])
    controller = make_controller(control, tmp_path)
    thread = threading.Thread(target=controller.run_forever)
    thread.start()
    for _ in range(100):
        if running(controller):
            break
        threading.Event().wait(0.05)

    assert running(controller) == {"a"}
    controller.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert running(controller) == set()


def test_run_forever_raises_the_fatal_error(tmp_path):
    control = FakeControl()
    control.list_error = ControlPlaneAuthError("refused")
    controller = make_controller(control, tmp_path)

    with pytest.raises(ControlPlaneAuthError):
        controller.run_forever()


def test_shared_executor_is_a_pool(tmp_path):
    controller = make_controller(FakeControl(), tmp_path)

    assert isinstance(controller._executor, ThreadPoolExecutor)


# -- fix round 1 ---------------------------------------------------------------------------------


class SlowRuntime(FakeRuntime):
    clock = None

    def request_stop(self):
        self.events.append("request_stop")

    def join(self, timeout):
        # Each join would burn its whole allowance; the shared deadline must cut the total.
        self.events.append(f"join:{timeout}")
        SlowRuntime.clock[0] += max(timeout, 0)

    def stop(self):
        self.request_stop()
        self.join(10)
        self.drop_credentials()


def test_shutdown_signals_all_first_then_joins_against_one_deadline(tmp_path, caplog):
    now = [0.0]
    SlowRuntime.clock = now
    rows = [row(f"g{i}") for i in range(10)]
    controller = make_controller(
        FakeControl(rows), tmp_path, clock=lambda: now[0], shutdown_seconds=25.0
    )
    controller._runtime_factory = SlowRuntime
    controller.reconcile_once()
    runtimes = list(SlowRuntime.created)
    assert len(runtimes) == 10
    for runtime in runtimes:
        runtime.events.clear()
    start = now[0]

    with caplog.at_level(logging.WARNING):
        controller._shutdown_runtimes()

    assert now[0] - start <= 25.0
    assert all(r.events[0] == "request_stop" for r in runtimes)
    assert all(r.mail is None for r in runtimes)
    assert any("10 of 10" in r.getMessage() for r in caplog.records)
    assert controller.runtimes == {}


def test_refresh_credentials_lost_list_is_applied_by_next_reconcile(tmp_path):
    control = FakeControl([row("a")])
    fetches = []

    def fetch(vault_uri, secret_name):
        fetches.append(secret_name)
        return fake_fetch(vault_uri, secret_name)

    controller = make_controller(control, tmp_path, fetch=fetch)
    controller.reconcile_once()
    assert len(fetches) == 1

    calls = {"n": 0}
    real = control.list_registrations

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ControlPlaneUnreachable("down")
        return real()

    control.list_registrations = flaky

    controller.handle_command({"id": "c1", "type": "refresh_credentials", "gatewayId": "a"})
    assert len(FakeRuntime.created) == 1 and FakeRuntime.created[0].alive  # old one untouched

    controller.reconcile_once()

    assert len(fetches) == 2
    assert FakeRuntime.created[0].events[-1] == "stopped"
    assert len(FakeRuntime.created) == 2 and FakeRuntime.created[1].alive
    assert controller._pending_restart == set()

    controller.reconcile_once()
    assert len(FakeRuntime.created) == 2  # applied once only


def test_probe_runs_on_the_probe_pool_not_the_job_pool(tmp_path):
    names = []

    def tester(settings):
        names.append(threading.current_thread().name)
        return {}

    run_test_credentials(tmp_path, GOOD_CREDENTIALS, tester)

    assert names and names[0].startswith("qlar-probe")


def test_probe_that_hangs_posts_a_timeout_error_once(tmp_path):
    release = threading.Event()

    def tester(settings):
        release.wait(5)
        return {"imapBanner": "late"}

    control = FakeControl()
    controller = make_controller(control, tmp_path, mailbox_tester=tester, probe_timeout_seconds=0.1)
    controller.handle_command({"id": "ctl_1", "type": "test_credentials", "credentials": GOOD_CREDENTIALS})
    for _ in range(100):
        if control.results:
            break
        threading.Event().wait(0.05)
    release.set()
    controller._probe_executor.shutdown(wait=True)

    assert len(control.results) == 1
    payload = control.results[0][1]
    assert payload["status"] == "error" and payload["error"]["category"] == "timeout"
    assert payload["error"]["messageText"]


def test_probe_without_an_id_posts_nothing(tmp_path, caplog):
    control = FakeControl()
    controller = make_controller(control, tmp_path, mailbox_tester=lambda s: {})

    with caplog.at_level(logging.ERROR):
        controller.handle_command({"type": "test_credentials", "credentials": GOOD_CREDENTIALS})
    controller._probe_executor.shutdown(wait=True)

    assert control.results == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


class StubbornRuntime(FakeRuntime):
    def stop(self):
        self.drop_credentials()  # threads refuse to die

    @property
    def is_alive(self):
        return self.alive


def test_runtime_that_will_not_die_blocks_a_new_one_for_the_same_id(tmp_path, caplog):
    control = FakeControl([row("a")])
    controller = make_controller(control, tmp_path)
    controller._runtime_factory = StubbornRuntime
    controller.reconcile_once()
    stubborn = FakeRuntime.created[0]

    with caplog.at_level(logging.WARNING):
        controller.handle_command({"id": "c1", "type": "refresh_credentials", "gatewayId": "a"})

    assert len(FakeRuntime.created) == 1  # refused: old threads still alive
    assert any("a" in r.getMessage() and "did not stop" in r.getMessage() for r in caplog.records)

    controller.reconcile_once()
    assert len(FakeRuntime.created) == 1

    stubborn.alive = False
    controller.reconcile_once()
    assert len(FakeRuntime.created) == 2
