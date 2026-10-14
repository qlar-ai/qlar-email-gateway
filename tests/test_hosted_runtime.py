import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from qlar_email_gateway.client import Deleted
from qlar_email_gateway.config import EnrollmentState, MailSettings
from qlar_email_gateway.enroll import EnrollmentCodeRejected, EnrollmentError
from qlar_email_gateway.hosted import runtime as runtime_module
from qlar_email_gateway.hosted.runtime import GatewayAuditLog, MailboxRuntime, build_settings

BASE = "https://q/api/email-gateway"


def make_mail() -> MailSettings:
    return MailSettings(
        imap_host="imap.x", smtp_host="smtp.x", user="u", password="p", address="a@x.com"
    )


def make_state(gateway_id: str = "egw_1") -> EnrollmentState:
    return EnrollmentState(
        gateway_id=gateway_id, qlar_public_key_pem="pem", enrolled_at="now", base_url=BASE
    )


class FakeControl:
    def __init__(self, rows=None, rekey_code="rekeyed"):
        self.rows = rows or []
        self.rekey_code = rekey_code
        self.rekeyed: list[str] = []
        self.list_calls = 0

    def rekey(self, gateway_id):
        self.rekeyed.append(gateway_id)
        return self.rekey_code

    def list_registrations(self):
        self.list_calls += 1
        return self.rows


class FakeEnroll:
    def __init__(self, reject_codes=()):
        self.reject_codes = set(reject_codes)
        self.calls: list[tuple[str | None, bool]] = []

    def __call__(self, settings, idle_probe=None, *, replace_existing=False):
        self.calls.append((settings.enrollment_code, replace_existing))
        if settings.enrollment_code in self.reject_codes:
            raise EnrollmentCodeRejected("code rejected")
        return make_state(), "fp"


def make_runtime(tmp_path, registration, control, enroll_fn, executor=None):
    return MailboxRuntime(
        registration,
        make_mail(),
        BASE,
        tmp_path,
        control,
        executor or ThreadPoolExecutor(max_workers=1),
        enroll_fn=enroll_fn,
    )


def write_local_enrollment(tmp_path: Path, gateway_id: str = "egw_1") -> None:
    folder = tmp_path / gateway_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "gateway-key.pem").write_text("key")
    (folder / "gateway-state.json").write_text(
        json.dumps(
            {
                "gatewayId": gateway_id,
                "qlarPublicKeyPem": "pem",
                "enrolledAt": "now",
                "baseUrl": BASE,
            }
        )
    )


def enrolled_runtime(tmp_path, executor=None):
    write_local_enrollment(tmp_path)
    runtime = make_runtime(
        tmp_path, {"gatewayId": "egw_1", "enrolled": True}, FakeControl(), FakeEnroll(), executor
    )
    runtime.ensure_enrolled()
    return runtime


def test_build_settings_uses_per_mailbox_paths_and_name(tmp_path):
    settings = build_settings("egw_1", BASE, tmp_path, make_mail(), "CODE")

    assert settings.key_file == tmp_path / "egw_1" / "gateway-key.pem"
    assert settings.state_file == tmp_path / "egw_1" / "gateway-state.json"
    assert settings.audit_log_file == tmp_path / "egw_1" / "audit" / "mail.jsonl"
    assert settings.gateway_name == "hosted:a@x.com"
    assert settings.enrollment_code == "CODE"
    assert settings.base_url == BASE


def test_already_enrolled_locally_does_nothing(tmp_path):
    write_local_enrollment(tmp_path)
    control, enroll_fn = FakeControl(), FakeEnroll()
    runtime = make_runtime(tmp_path, {"gatewayId": "egw_1", "enrolled": True}, control, enroll_fn)

    runtime.ensure_enrolled()

    assert enroll_fn.calls == [] and control.rekeyed == []


def test_not_enrolled_uses_registration_code(tmp_path):
    control, enroll_fn = FakeControl(), FakeEnroll()
    registration = {"gatewayId": "egw_1", "enrolled": False, "enrollmentCode": "FRESH"}

    make_runtime(tmp_path, registration, control, enroll_fn).ensure_enrolled()

    assert control.rekeyed == []
    assert [code for code, _ in enroll_fn.calls] == ["FRESH"]


def test_server_enrolled_but_local_key_missing_rekeys_then_reenrolls(tmp_path):
    control, enroll_fn = FakeControl(rekey_code="NEWCODE"), FakeEnroll()
    registration = {"gatewayId": "egw_1", "enrolled": True, "enrollmentCode": None}

    make_runtime(tmp_path, registration, control, enroll_fn).ensure_enrolled()

    assert control.rekeyed == ["egw_1"]
    assert enroll_fn.calls == [("NEWCODE", True)]


def test_stale_code_is_refetched_once_and_retried_once(tmp_path):
    control = FakeControl(
        rows=[{"gatewayId": "egw_1", "enrolled": False, "enrollmentCode": "FRESH"}]
    )
    enroll_fn = FakeEnroll(reject_codes={"STALE"})
    registration = {"gatewayId": "egw_1", "enrolled": False, "enrollmentCode": "STALE"}

    make_runtime(tmp_path, registration, control, enroll_fn).ensure_enrolled()

    assert control.list_calls == 1
    assert [code for code, _ in enroll_fn.calls] == ["STALE", "FRESH"]


def test_code_rejected_twice_raises_without_a_third_attempt(tmp_path):
    control = FakeControl(
        rows=[{"gatewayId": "egw_1", "enrolled": False, "enrollmentCode": "ALSO_BAD"}]
    )
    enroll_fn = FakeEnroll(reject_codes={"STALE", "ALSO_BAD"})
    registration = {"gatewayId": "egw_1", "enrolled": False, "enrollmentCode": "STALE"}

    with pytest.raises(EnrollmentError):
        make_runtime(tmp_path, registration, control, enroll_fn).ensure_enrolled()

    assert control.list_calls == 1 and len(enroll_fn.calls) == 2


def test_gateway_audit_log_stamps_gateway_id(tmp_path):
    path = tmp_path / "audit.jsonl"

    GatewayAuditLog(path, "egw_9").record(status="forwarded")

    assert json.loads(path.read_text().splitlines()[0])["gatewayId"] == "egw_9"


class FakeLoop:
    instances: list["FakeLoop"] = []

    def __init__(self, settings, state, *, status=None, audit=None, executor=None, **_):
        self.settings, self.state, self.audit, self.executor = settings, state, audit, executor
        self.client = object()
        self.guard = object()
        self.stopped_by = None
        self._stop = threading.Event()
        self.started = threading.Event()
        FakeLoop.instances.append(self)

    def run_forever(self):
        self.started.set()
        self._stop.wait(5)

    def stop(self):
        self._stop.set()


class FakeWatcher:
    instances: list["FakeWatcher"] = []
    raise_on_run: Exception | None = None

    def __init__(self, settings, state, client, audit, guard, status, **kwargs):
        self.kwargs, self.audit = kwargs, audit
        self._stop = threading.Event()
        FakeWatcher.instances.append(self)

    def run_forever(self):
        if FakeWatcher.raise_on_run:
            raise FakeWatcher.raise_on_run
        self._stop.wait(5)

    def stop(self):
        self._stop.set()


@pytest.fixture
def fakes(monkeypatch):
    FakeLoop.instances, FakeWatcher.instances = [], []
    FakeWatcher.raise_on_run = None
    monkeypatch.setattr(runtime_module, "PollLoop", FakeLoop)
    monkeypatch.setattr(runtime_module, "MailboxWatcher", FakeWatcher)


def test_start_stop_round_trip(tmp_path, fakes):
    executor = ThreadPoolExecutor(max_workers=1)
    runtime = enrolled_runtime(tmp_path, executor)

    runtime.start()
    loop, watcher = FakeLoop.instances[0], FakeWatcher.instances[0]
    assert loop.started.wait(2)
    assert runtime.is_alive
    names = {t.name for t in threading.enumerate()}
    assert {"qlar-mailbox:egw_1", "qlar-poll:egw_1"} <= names
    assert loop.executor is executor
    assert watcher.kwargs["login_retry_schedule"] == (60, 120, 300, 900)
    assert isinstance(watcher.audit, GatewayAuditLog) and watcher.audit is loop.audit

    runtime.stop()

    assert not runtime.is_alive
    assert runtime.stopped_by is None


def test_watcher_deletion_stops_runtime_and_reports_it(tmp_path, fakes):
    FakeWatcher.raise_on_run = Deleted()
    runtime = enrolled_runtime(tmp_path)

    runtime.start()
    for _ in range(100):
        if not runtime.is_alive:
            break
        threading.Event().wait(0.05)

    assert not runtime.is_alive
    assert runtime.stopped_by == "deleted"


def test_stop_drops_credentials(tmp_path, fakes):
    runtime = enrolled_runtime(tmp_path)
    runtime.start()

    runtime.stop()

    assert runtime.mail is None and runtime.settings is None
