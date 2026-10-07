"""The IMAP watcher: which mail is forwarded, in what order, and what survives a failure.

Everything runs against a fake IMAP server and a fake Qlar, with sleeps and the clock injected,
so the scenarios that take minutes in real life (an hour of backlog, IDLE dropping every few
minutes) run instantly.
"""

from __future__ import annotations

import imaplib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from imapclient.exceptions import LoginError

from qlar_email_gateway.audit import AuditLog
from qlar_email_gateway.client import QlarRejected, QlarUnreachable, Revoked
from qlar_email_gateway.config import EnrollmentState, MailSettings, Settings
from qlar_email_gateway.mailbox import MailboxWatcher
from qlar_email_gateway.send_guard import SendGuard
from qlar_email_gateway.status import MailboxStatus

MAILBOX = "ask@corp.test"


def raw_email(uid: int, **headers: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = headers.pop("From", f"Sender {uid} <sender{uid}@customer.test>")
    msg["To"] = MAILBOX
    msg["Subject"] = f"Question {uid}"
    msg["Date"] = "Wed, 07 Oct 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<m{uid}@customer.test>"
    for key, value in headers.items():
        msg[key.replace("_", "-")] = value
    msg.set_content(f"Body of {uid}")
    return msg.as_bytes()


class FakeImap:
    """The subset of imapclient.IMAPClient the watcher uses."""

    def __init__(
        self, messages=None, uidvalidity=1, uidnext=None, capabilities=(b"IMAP4REV1",), login_error=None
    ):
        self.messages: dict[int, bytes] = dict(messages or {})
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext if uidnext is not None else (max(self.messages, default=0) + 1)
        self._capabilities = capabilities
        self.login_error = login_error
        self.selected_readonly: list[bool] = []
        self.fetch_items: list = []
        self.calls: list[str] = []
        self.idle_check_effect = None

    def login(self, user, password):
        self.calls.append("login")
        if self.login_error:
            raise self.login_error

    def capabilities(self):
        return self._capabilities

    def select_folder(self, folder, readonly=False):
        self.selected_readonly.append(readonly)
        return {b"UIDVALIDITY": self.uidvalidity, b"UIDNEXT": self.uidnext, b"EXISTS": len(self.messages)}

    def search(self, criteria):
        assert criteria[0] == "UID"
        start = int(criteria[1].split(":")[0])
        found = [uid for uid in self.messages if uid >= start]
        # Real servers answer "n:*" with the highest UID when nothing is that new.
        return found or ([max(self.messages)] if self.messages else [])

    def fetch(self, uids, items):
        self.fetch_items.append(items)
        return {uid: {b"BODY[]": self.messages[uid]} for uid in uids}

    def noop(self):
        self.calls.append("noop")

    def idle(self):
        self.calls.append("idle")

    def idle_check(self, timeout=None):
        self.calls.append("idle_check")
        if self.idle_check_effect is not None:
            return self.idle_check_effect()
        return []

    def idle_done(self):
        self.calls.append("idle_done")

    def logout(self):
        self.calls.append("logout")


class FakeQlar:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.posts: list[dict] = []
        self.on_post = None

    def post(self, path, payload, *, timeout=30.0):
        assert path == "/inbound"
        self.posts.append(payload)
        if self.on_post:
            self.on_post(payload)
        outcome = self.script.pop(0) if self.script else (202, {"accepted": True})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Harness:
    def __init__(self, tmp_path, imap: FakeImap, qlar: FakeQlar | None = None, **setting_overrides):
        self.now = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
        self.sleeps: list[float] = []
        self.stop_when = lambda seconds: False
        self.settings = replace(
            Settings(
                base_url="https://q/api/email-gateway",
                gateway_name="g",
                enrollment_code=None,
                key_file=tmp_path / "key.pem",
                state_file=tmp_path / "state.json",
                audit_log_file=tmp_path / "audit" / "mail.jsonl",
                poll_timeout_seconds=25,
                verify_tls=True,
                mail=MailSettings(
                    imap_host="imap", smtp_host="smtp", user=MAILBOX, password="pw", address=MAILBOX
                ),
            ),
            **setting_overrides,
        )
        self.state = EnrollmentState(
            gateway_id="egw_1",
            qlar_public_key_pem="PEM",
            enrolled_at="2026-10-07T08:00:00+00:00",
            base_url="b",
            mailbox_address=MAILBOX,
            uid_validity=1,
            last_uid=10,
        )
        self.imap = imap
        self.qlar = qlar or FakeQlar()
        self.status = MailboxStatus()
        self.guard = SendGuard(self.settings, self.state, clock=lambda: self.now)
        self.watcher = MailboxWatcher(
            self.settings,
            self.state,
            self.qlar,
            AuditLog(self.settings.audit_log_file),
            self.guard,
            self.status,
            imap_factory=lambda mail: self.imap,
            clock=lambda: self.now,
            sleep=self._sleep,
        )

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.stop_when(seconds):
            self.watcher.stop()

    def run_until_poll_wait(self) -> None:
        """Runs until the watcher settles into waiting for new mail (no-IDLE server)."""
        interval = self.settings.poll_interval_seconds
        self.stop_when = lambda seconds: seconds == interval
        self.watcher.run_forever()

    def audit(self) -> list[dict]:
        path = self.settings.audit_log_file
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_first_run_starts_at_uidnext_and_answers_nothing_old(tmp_path):
    imap = FakeImap({99: raw_email(99), 100: raw_email(100)}, uidnext=101)
    harness = Harness(tmp_path, imap)
    harness.state.last_uid = None
    harness.state.uid_validity = None

    harness.run_until_poll_wait()

    assert harness.qlar.posts == []
    assert harness.state.last_uid == 100
    assert EnrollmentState.load(harness.settings.state_file).last_uid == 100
    assert [entry["status"] for entry in harness.audit()] == ["uidvalidity_reset"]


def test_uidvalidity_change_resets_and_audits(tmp_path):
    imap = FakeImap({5: raw_email(5)}, uidvalidity=6, uidnext=10)
    harness = Harness(tmp_path, imap)
    harness.state.uid_validity = 5
    harness.state.last_uid = 50

    harness.run_until_poll_wait()

    assert (harness.state.uid_validity, harness.state.last_uid) == (6, 9)
    assert harness.qlar.posts == []
    assert harness.audit()[0]["status"] == "uidvalidity_reset"


def test_backlog_forwarded_in_order_and_last_uid_only_after_202(tmp_path):
    imap = FakeImap({11: raw_email(11), 12: raw_email(12), 13: raw_email(13)})
    qlar = FakeQlar([QlarUnreachable("down"), QlarRejected(503, "unavailable"), (202, {"accepted": True})])
    harness = Harness(tmp_path, imap, qlar)
    last_uid_at_post: list[int | None] = []
    qlar.on_post = lambda payload: last_uid_at_post.append(harness.state.last_uid)

    harness.run_until_poll_wait()

    assert [payload["uid"] for payload in qlar.posts] == [11, 11, 11, 12, 13]
    assert last_uid_at_post == [10, 10, 10, 11, 12]
    assert harness.state.last_uid == 13
    assert harness.sleeps[:2] == [1, 2], "Qlar failures back off 1, 2 … seconds"
    assert [entry["status"] for entry in harness.audit()] == ["forwarded", "forwarded", "forwarded"]


def test_qlar_400_advances_and_audits(tmp_path):
    imap = FakeImap({11: raw_email(11), 12: raw_email(12)})
    qlar = FakeQlar([QlarRejected(400, "messageId and from.address are required"), (202, {})])
    harness = Harness(tmp_path, imap, qlar)

    harness.run_until_poll_wait()

    assert [payload["uid"] for payload in qlar.posts] == [11, 12]
    assert harness.state.last_uid == 12
    first = harness.audit()[0]
    assert (first["status"], first["messageId"]) == ("rejected_by_qlar", "<m11@customer.test>")


def test_filtered_mail_advances_without_posting(tmp_path):
    imap = FakeImap({11: raw_email(11, Auto_Submitted="auto-replied")})
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert harness.qlar.posts == []
    assert harness.state.last_uid == 11
    entry = harness.audit()[0]
    assert (entry["status"], entry["reason"]) == ("filtered", "auto_submitted")


def test_forwarded_sender_and_reply_to_are_remembered(tmp_path):
    imap = FakeImap({11: raw_email(11, Reply_To="Team@Customer.Test")})
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert set(harness.state.recipients) == {"sender11@customer.test", "team@customer.test"}


def test_uses_body_peek_never_sets_seen(tmp_path):
    imap = FakeImap({11: raw_email(11)})
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert imap.selected_readonly and all(imap.selected_readonly)
    assert imap.fetch_items == [["BODY.PEEK[]"]]


def test_pending_approval_waits_and_retries_same_uid(tmp_path):
    imap = FakeImap({11: raw_email(11)})
    qlar = FakeQlar([QlarRejected(403, "pending", {"reason": "pending_approval"}), (202, {})])
    harness = Harness(tmp_path, imap, qlar)

    harness.run_until_poll_wait()

    assert [payload["uid"] for payload in qlar.posts] == [11, 11]
    assert harness.sleeps[0] == 4
    assert harness.state.last_uid == 11


def test_revoked_stops(tmp_path):
    imap = FakeImap({11: raw_email(11)})
    qlar = FakeQlar([QlarRejected(403, "revoked", {"reason": "revoked"})])
    harness = Harness(tmp_path, imap, qlar)

    with pytest.raises(Revoked):
        harness.run_until_poll_wait()

    assert harness.state.last_uid == 10


def test_login_failure_sets_auth_failed(tmp_path):
    imap = FakeImap({}, login_error=LoginError("AUTHENTICATIONFAILED"))
    harness = Harness(tmp_path, imap)
    harness.stop_when = lambda seconds: True

    harness.watcher.run_forever()

    assert harness.status.get() == "auth_failed"
    assert harness.sleeps == [60]


def test_unreachable_server_sets_unreachable_and_backs_off(tmp_path):
    imap = FakeImap({})
    harness = Harness(tmp_path, imap)

    def refuse(mail):
        raise ConnectionRefusedError(111, "refused")

    harness.watcher.imap_factory = refuse
    harness.stop_when = lambda seconds: len(harness.sleeps) == 5

    harness.watcher.run_forever()

    assert harness.status.get() == "unreachable"
    assert [int(seconds) for seconds in harness.sleeps] == [1, 2, 4, 8, 15]


def test_flapping_idle_falls_back_to_polling(tmp_path):
    imap = FakeImap({}, capabilities=(b"IMAP4REV1", b"IDLE"))
    imap.uidnext = 11

    def drop():
        raise imaplib.IMAP4.abort("connection reset by server")

    imap.idle_check_effect = drop
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert imap.calls.count("idle") == 4, "the fourth drop within five minutes switches to polling"
    assert "noop" in imap.calls
    assert harness.status.get() == "idle_unsupported"


def test_idle_new_mail_is_fetched(tmp_path):
    imap = FakeImap({}, capabilities=(b"IMAP4REV1", b"IDLE"))
    imap.uidnext = 11
    harness = Harness(tmp_path, imap)
    checks = {"count": 0}

    def arrive():
        checks["count"] += 1
        if checks["count"] == 1:
            imap.messages[11] = raw_email(11)
            return [(11, b"EXISTS")]
        harness.watcher.stop()
        return []

    imap.idle_check_effect = arrive

    harness.watcher.run_forever()

    assert [payload["uid"] for payload in harness.qlar.posts] == [11]
    assert harness.status.get() == "ok"


def test_no_idle_capability_polls_at_interval(tmp_path):
    imap = FakeImap({}, uidnext=11)
    harness = Harness(tmp_path, imap, poll_interval_seconds=45)

    harness.run_until_poll_wait()

    assert harness.sleeps[-1] == 45
    assert "idle" not in imap.calls
    assert harness.status.get() == "ok"


# -- review fixes ---------------------------------------------------------------------------------

MALFORMED = (
    b'From: someone@customer.test\r\nTo: "\r\nSubject: broken\r\n'
    b"Message-ID: <m11@customer.test>\r\nDate: Wed, 07 Oct 2026 09:00:00 +0000\r\n\r\nbody\r\n"
)


def test_an_unparseable_email_is_audited_and_skipped(tmp_path):
    # Python's header parser raises IndexError on `To: "`; one such email must not stop the watcher.
    imap = FakeImap({11: MALFORMED, 12: raw_email(12)})
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert [payload["uid"] for payload in harness.qlar.posts] == [12]
    assert harness.state.last_uid == 12
    first = harness.audit()[0]
    assert (first["status"], first["uid"]) == ("unparseable", 11)


def test_idle_that_returns_at_once_counts_as_a_drop(tmp_path):
    # Real imapclient returns [] immediately when the server closes the connection during IDLE.
    imap = FakeImap({}, capabilities=(b"IMAP4REV1", b"IDLE"))
    imap.uidnext = 11
    imap.idle_check_effect = lambda: []
    harness = Harness(tmp_path, imap)

    harness.run_until_poll_wait()

    assert imap.calls.count("idle_check") == 4, "each immediate return is a drop, not a spin"
    assert harness.status.get() == "idle_unsupported"


def test_bye_during_idle_counts_as_a_drop(tmp_path):
    imap = FakeImap({}, capabilities=(b"IMAP4REV1", b"IDLE"))
    imap.uidnext = 11
    harness = Harness(tmp_path, imap)

    def bye():
        harness.now += timedelta(seconds=5)
        return [(b"BYE", b"Server shutting down")]

    imap.idle_check_effect = bye

    harness.run_until_poll_wait()

    assert harness.status.get() == "idle_unsupported"


def test_mail_arriving_during_a_sync_is_fetched_without_waiting_for_more_mail(tmp_path):
    imap = FakeImap({11: raw_email(11)}, capabilities=(b"IMAP4REV1", b"IDLE"))
    imap.uidnext = 11
    harness = Harness(tmp_path, imap)
    harness.state.last_uid = 10

    def arrive_during_post(payload):
        if payload["uid"] == 11:
            imap.messages[12] = raw_email(12)  # its EXISTS notice is lost: it came during the POST
        if payload["uid"] == 12:
            harness.watcher.stop()

    harness.qlar.on_post = arrive_during_post

    def quiet_minute():
        harness.now += timedelta(seconds=60)
        return []

    imap.idle_check_effect = quiet_minute

    harness.watcher.run_forever()

    assert [payload["uid"] for payload in harness.qlar.posts] == [11, 12]


def test_a_state_file_that_cannot_be_written_does_not_look_like_a_network_failure(tmp_path, monkeypatch):
    imap = FakeImap({11: raw_email(11), 12: raw_email(12)})
    harness = Harness(tmp_path, imap)

    def disk_full(self, path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(EnrollmentState, "save", disk_full)

    harness.run_until_poll_wait()

    assert [payload["uid"] for payload in harness.qlar.posts] == [11, 12]
    assert harness.status.get() == "ok"
