"""The hosted worker's composition root: which mailboxes run, and what the control plane asks of them.

Two threads of its own. The reconcile loop compares Qlar's list of hosted registrations with the
runtimes it holds, every `RECONCILE_SECONDS`; the control loop long-polls for commands that should
not wait for the next reconcile (a new mailbox, new credentials, a credentials test).
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import mailbox as mailbox_module
from ..config import MailSettings, Settings
from ..sender import SmtpSendError
from .api import (
    ControlPlaneAuthError,
    ControlPlaneClient,
    ControlPlaneRejected,
    ControlPlaneUnreachable,
)
from .credentials import CredentialFormatError, fetch_mail_settings, parse_mail_settings
from .runtime import MailboxRuntime, build_settings

logger = logging.getLogger("qlar_email_gateway.hosted.controller")

RECONCILE_SECONDS = 30
MAX_MAILBOXES = 200
EXECUTOR_WORKERS = 8
PROBE_WORKERS = 2
#: One deadline for the whole shutdown, under a container's usual 30 s stop timeout.
SHUTDOWN_JOIN_SECONDS = 25.0
#: Under Messenger-BE's 30 s wait for a credentials test, so the timeout report still arrives.
PROBE_TIMEOUT_SECONDS = 25.0
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

REVOKED = "revoked"
#: `MailboxRuntime.stopped_by` when Qlar answered `unknown_gateway`: it holds no key for this id.
DELETED = "deleted"
#: A gateway id is used as a directory name, so nothing else is ever deleted.
_SAFE_GATEWAY_ID = re.compile(r"[A-Za-z0-9_-]+")


class HostedController:
    def __init__(
        self,
        control: ControlPlaneClient,
        *,
        vault_uri: str,
        base_url: str,
        state_dir: Path,
        fetch_credentials: Callable[[str, str], MailSettings] = fetch_mail_settings,
        runtime_factory: Callable[..., Any] = MailboxRuntime,
        mailbox_tester: Callable[[Settings], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        shutdown_seconds: float = SHUTDOWN_JOIN_SECONDS,
        probe_timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self._control = control
        self._vault_uri = vault_uri
        self._base_url = base_url
        self._state_dir = state_dir
        self._fetch_credentials = fetch_credentials
        self._runtime_factory = runtime_factory
        self._mailbox_tester = mailbox_tester
        self._executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS, thread_name_prefix="qlar-job")
        self._clock = clock
        self._shutdown_seconds = shutdown_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        # Credential probes take as long as a customer's mail server does; on their own small pool
        # they cannot hold up the jobs of the running mailboxes.
        self._probe_executor = ThreadPoolExecutor(
            max_workers=PROBE_WORKERS, thread_name_prefix="qlar-probe"
        )
        self.runtimes: dict[str, Any] = {}
        #: Runtimes that outlived their stop request: their state file is still in use, so no new
        #: runtime for the same id may start until they are dead.
        self._lingering: dict[str, Any] = {}
        #: Runtimes whose `stop()` is in flight (it joins for seconds, outside the lock). Their id
        #: is in neither `runtimes` nor `_lingering` yet, so this is what keeps a second runtime off
        #: the same state files meanwhile.
        self._stopping: dict[str, Any] = {}
        #: configure/refresh commands that could not be applied yet; the next reconcile does it.
        self._pending_restart: set[str] = set()
        #: Mailboxes whose runtime Qlar stopped as revoked while the list still showed them: left
        #: alone until they leave the list. (A deletion while still listed re-enrols instead.)
        self._halted: set[str] = set()
        #: Mailboxes being started right now (vault read, enrolment), outside the lock.
        self._starting: set[str] = set()
        #: Removed mailboxes whose /state/{id} could not be deleted yet (runtime still running).
        self._purge_pending: set[str] = set()
        #: Guards the maps and sets above. Held only to read or change them, never across a vault
        #: read, an enrolment or a join, so a stop is never stuck behind a long start loop.
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        #: Set when the control plane refused the worker key: a deployment error, so the process ends.
        self.fatal: Exception | None = None

    # -- lifecycle -----------------------------------------------------------------------------

    def run_forever(self) -> None:
        threads = [
            threading.Thread(target=self._reconcile_loop, name="qlar-reconcile", daemon=True),
            threading.Thread(target=self._control_loop, name="qlar-control", daemon=True),
        ]
        for thread in threads:
            thread.start()

        self._stopped.wait()

        self._shutdown_runtimes()
        self._probe_executor.shutdown(wait=False, cancel_futures=True)
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self.fatal is not None:
            raise self.fatal

    def stop(self) -> None:
        self._stopped.set()

    def _shutdown_runtimes(self) -> None:
        """Tells every runtime to stop at once, then waits for all of them against one deadline.

        `_stopped` is already set, so no start in flight can add a runtime after the map is taken.
        """
        with self._lock:
            runtimes = list(self.runtimes.values())
            self.runtimes.clear()

        for runtime in runtimes:
            try:
                runtime.request_stop()
            except Exception as error:  # noqa: BLE001 - one runtime must not keep the rest running
                logger.warning(
                    "could not signal mailbox %s to stop (%s)",
                    getattr(runtime, "gateway_id", "?"),
                    type(error).__name__,
                )

        deadline = self._clock() + self._shutdown_seconds
        for runtime in runtimes:
            try:
                runtime.join(max(0.0, deadline - self._clock()))
            except Exception as error:  # noqa: BLE001 - keep waiting for the others
                logger.warning(
                    "could not wait for mailbox %s (%s)",
                    getattr(runtime, "gateway_id", "?"),
                    type(error).__name__,
                )
            runtime.drop_credentials()

        alive = sum(1 for runtime in runtimes if runtime.is_alive)
        if alive:
            logger.warning(
                "%d of %d mailbox runtimes were still running at the shutdown deadline",
                alive,
                len(runtimes),
            )

    # -- reconcile -----------------------------------------------------------------------------

    def reconcile_once(self) -> None:
        rows = self._control.list_registrations()
        if self._stopped.is_set():
            return  # stopped while the list was in flight: start nothing

        with self._lock:
            desired = self._desired(rows)
            gone = [g for g in self.runtimes if g not in desired]
            left_halted = sorted(self._halted - desired.keys())
            self._halted &= desired.keys()
            self._pending_restart &= desired.keys()
            restarts = sorted(self._pending_restart)

        for gateway_id in gone:
            logger.info("mailbox %s is gone or revoked; stopping it", gateway_id)
            self._drop(gateway_id, forget=True)
        for gateway_id in left_halted:
            # Halted as revoked, and the row is gone now: its key and audit are no longer needed.
            self._forget_state(gateway_id)
        self._retry_state_purges(desired)

        attempted: set[str] = set()
        for gateway_id in restarts:
            if self._stopped.is_set():
                return
            self._drop(gateway_id)
            with self._lock:
                self._halted.discard(gateway_id)
            attempted.add(gateway_id)
            if self._start(desired[gateway_id]):
                with self._lock:
                    self._pending_restart.discard(gateway_id)

        with self._lock:
            dead = [(g, r) for g, r in self.runtimes.items() if not r.is_alive]
        for gateway_id, runtime in dead:
            reason = runtime.stopped_by
            self._drop(gateway_id)
            if reason == DELETED:
                # Qlar holds no key for this id, yet still lists it as a live hosted mailbox (a
                # rekey, or a lost update that left it un-enrolled). The local key is useless:
                # forget it and enrol again below, rather than halting the mailbox forever.
                logger.warning(
                    "mailbox %s: Qlar no longer knows its key but still lists it; wiping its local "
                    "state and enrolling again",
                    gateway_id,
                )
                self._forget_state(gateway_id)
            elif reason:
                logger.error("mailbox %s was stopped by Qlar (%s); not restarting it", gateway_id, reason)
                with self._lock:
                    self._halted.add(gateway_id)
            else:
                logger.warning("mailbox %s stopped unexpectedly; restarting it", gateway_id)

        for gateway_id, registration in desired.items():
            if self._stopped.is_set():
                return
            with self._lock:
                skip = (
                    gateway_id in self.runtimes
                    or gateway_id in self._halted
                    or gateway_id in attempted
                )
            if not skip:
                self._start(registration)

    def _desired(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        hosted = [
            row for row in rows if row.get("gatewayId") and str(row.get("status", "")).lower() != REVOKED
        ]
        if len(hosted) > MAX_MAILBOXES:
            logger.error(
                "%d hosted mailboxes registered but this worker serves at most %d; refusing the %d "
                "beyond the limit",
                len(hosted),
                MAX_MAILBOXES,
                len(hosted) - MAX_MAILBOXES,
            )
            hosted = hosted[:MAX_MAILBOXES]
        return {str(row["gatewayId"]): row for row in hosted}

    def _start(self, registration: dict[str, Any]) -> bool:
        """Starts one mailbox. The lock is held only to claim the id and to publish the runtime."""
        gateway_id = str(registration["gatewayId"])
        with self._lock:
            if self._stopped.is_set() or gateway_id in self.runtimes or gateway_id in self._starting:
                return False
            if gateway_id in self._stopping:
                logger.warning(
                    "not starting mailbox %s: its previous runtime is still stopping", gateway_id
                )
                return False
            lingering = self._lingering.get(gateway_id)
            if lingering is not None:
                if lingering.is_alive:
                    logger.warning(
                        "not starting mailbox %s: its previous runtime is still shutting down", gateway_id
                    )
                    return False
                del self._lingering[gateway_id]
            self._starting.add(gateway_id)
        try:
            return self._start_claimed(gateway_id, registration)
        finally:
            with self._lock:
                self._starting.discard(gateway_id)

    def _start_claimed(self, gateway_id: str, registration: dict[str, Any]) -> bool:
        try:
            mail = self._fetch_credentials(self._vault_uri, str(registration.get("secretName") or ""))
        except CredentialFormatError as error:
            logger.error("skipping mailbox %s: %s", gateway_id, error)
            return False
        except Exception as error:  # noqa: BLE001 - the vault can fail in many SDK-specific ways
            # Type only: an SDK message can carry the vault address or request detail.
            logger.error(
                "skipping mailbox %s: could not read its credentials (%s)", gateway_id, type(error).__name__
            )
            return False
        if self._stopped.is_set():
            return False

        runtime = self._runtime_factory(
            registration, mail, self._base_url, self._state_dir, self._control, self._executor
        )
        started = False
        try:
            runtime.ensure_enrolled()
            with self._lock:
                # Checked under the lock the shutdown takes, so a runtime is either in the map the
                # shutdown stops, or never started at all.
                if not self._stopped.is_set():
                    runtime.start()
                    self.runtimes[gateway_id] = runtime
                    started = True
        except Exception as error:  # noqa: BLE001 - one mailbox must not take the others down
            logger.error(
                "skipping mailbox %s: could not start (%s: %s)", gateway_id, type(error).__name__, error
            )
            runtime.stop()
            return False

        if not started:
            logger.info("not starting mailbox %s: the worker is stopping", gateway_id)
            runtime.stop()
            return False
        logger.info("mailbox %s started", gateway_id)
        return True

    def _drop(self, gateway_id: str, *, forget: bool = False) -> None:
        """Stops a mailbox's runtime. `forget` also deletes its local state once it is dead."""
        with self._lock:
            runtime = self.runtimes.pop(gateway_id, None)
            if runtime is not None:
                # Marked in the same critical section as the pop: there is no moment at which the
                # id is in no collection while its threads may still be running.
                self._stopping[gateway_id] = runtime
        if runtime is not None:
            try:
                runtime.stop()
            finally:
                with self._lock:
                    if self._stopping.get(gateway_id) is runtime:
                        del self._stopping[gateway_id]
                    if runtime.is_alive:
                        self._lingering[gateway_id] = runtime
            if runtime.is_alive:
                logger.warning(
                    "mailbox %s did not stop in time; its threads are still running", gateway_id
                )
        if forget:
            self._forget_state(gateway_id)

    def _forget_state(self, gateway_id: str) -> None:
        """Deletes `{state_dir}/{gatewayId}` (private key, enrolment state, audit) of a dead runtime.

        While any runtime for the id is still running or starting, the deletion waits for a later
        reconcile: its files are still in use.
        """
        with self._lock:
            lingering = self._lingering.get(gateway_id)
            if (
                gateway_id in self.runtimes
                or gateway_id in self._starting
                or gateway_id in self._stopping
                or (lingering is not None and lingering.is_alive)
            ):
                self._purge_pending.add(gateway_id)
                return
            self._purge_pending.discard(gateway_id)
            self._lingering.pop(gateway_id, None)
        self._delete_state_dir(gateway_id)

    def _retry_state_purges(self, desired: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            pending = list(self._purge_pending)
        for gateway_id in pending:
            if gateway_id in desired:
                # Listed again: its state is live once more, so it is no longer to be deleted.
                with self._lock:
                    self._purge_pending.discard(gateway_id)
            else:
                self._forget_state(gateway_id)

    def _delete_state_dir(self, gateway_id: str) -> None:
        if not _SAFE_GATEWAY_ID.fullmatch(gateway_id):
            logger.error("not deleting the state of mailbox %r: not a gateway id", gateway_id)
            return
        folder = self._state_dir / gateway_id
        if not folder.exists():
            return
        try:
            shutil.rmtree(folder)
            logger.info("mailbox %s: deleted its local key and state", gateway_id)
        except OSError as error:
            logger.warning(
                "mailbox %s: could not delete its local state (%s)", gateway_id, type(error).__name__
            )

    # -- commands ------------------------------------------------------------------------------

    def handle_command(self, cmd: dict[str, Any]) -> None:
        kind = str(cmd.get("type", ""))
        gateway_id = str(cmd.get("gatewayId") or "")

        if kind in ("configure", "refresh_credentials"):
            self._restart(gateway_id)
        elif kind == "remove":
            if not gateway_id:
                logger.warning("ignoring a remove command that names no mailbox")
                return
            logger.info("mailbox %s is gone or revoked; stopping it", gateway_id)
            self._drop(gateway_id, forget=True)
            with self._lock:
                self._halted.discard(gateway_id)
                self._pending_restart.discard(gateway_id)
        elif kind == "test_credentials":
            self._start_probe(cmd)
        else:
            logger.warning("ignoring unknown control command %r", kind)

    def _restart(self, gateway_id: str) -> None:
        try:
            registration = next(
                (r for r in self._control.list_registrations() if str(r.get("gatewayId")) == gateway_id),
                None,
            )
            if self._stopped.is_set():
                return  # stopped while the list was in flight: start nothing
            nothing_to_run = (
                registration is None or str(registration.get("status", "")).lower() == REVOKED
            )
            self._drop(gateway_id, forget=nothing_to_run)
            with self._lock:
                self._halted.discard(gateway_id)
                self._pending_restart.discard(gateway_id)
                if registration is None or nothing_to_run:
                    logger.info("nothing to start for mailbox %s (gone or revoked)", gateway_id)
                    return
                if len(self.runtimes) >= MAX_MAILBOXES:
                    logger.error(
                        "not starting mailbox %s: the worker already serves %d", gateway_id, MAX_MAILBOXES
                    )
                    return
            if not self._start(registration):
                with self._lock:
                    if not self._stopped.is_set():
                        self._pending_restart.add(gateway_id)
        except Exception as error:  # noqa: BLE001 - the command is already consumed; reconcile finishes it
            logger.warning(
                "could not apply the restart of mailbox %s now (%s); the next reconcile will",
                gateway_id,
                type(error).__name__,
            )
            with self._lock:
                self._pending_restart.add(gateway_id)

    def _start_probe(self, cmd: dict[str, Any]) -> None:
        control_id = str(cmd.get("id") or cmd.get("controlId") or "")
        if not control_id:
            logger.error("ignoring a credentials test command that carries no id")
            return

        guard = threading.Lock()
        done = [False]

        def post_once(payload: dict[str, Any]) -> None:
            with guard:
                if done[0]:
                    return
                done[0] = True
            self._post_result(control_id, payload)

        timer = threading.Timer(
            self._probe_timeout_seconds,
            post_once,
            [_error("timeout", "probe_timeout", "the mail server did not answer in time")],
        )
        timer.daemon = True
        timer.start()

        def work() -> None:
            try:
                post_once(self._probe_payload(cmd, control_id))
            finally:
                timer.cancel()

        self._probe_executor.submit(work)

    def _probe_payload(self, cmd: dict[str, Any], control_id: str) -> dict[str, Any]:
        started = time.monotonic()
        tester = self._mailbox_tester or mailbox_module.test_mailbox
        try:
            mail = parse_mail_settings(json.dumps(cmd.get("credentials")))
            settings = build_settings("credentials-test", self._base_url, self._state_dir, mail, None)
            report = tester(settings)
            return {
                "status": "ok",
                "durationMs": int((time.monotonic() - started) * 1000),
                **report,
            }
        except CredentialFormatError as error:
            return _error("rejected", "invalid_credentials", str(error))
        except (mailbox_module.MailboxError, SmtpSendError) as error:
            return _error(error.category, str(getattr(error, "code", "")), str(error))
        except Exception as error:  # noqa: BLE001 - the waiting caller must always get an answer
            logger.exception("credentials test %s failed unexpectedly", control_id)
            return _error("internal", type(error).__name__, "the worker failed unexpectedly")

    def _post_result(self, control_id: str, payload: dict[str, Any]) -> None:
        try:
            self._control.post_result(control_id, payload)
        except Exception as error:  # noqa: BLE001 - the caller times out; nothing else to do
            logger.error(
                "could not post the result of credentials test %s (%s)", control_id, type(error).__name__
            )

    # -- loops ---------------------------------------------------------------------------------

    def _reconcile_loop(self) -> None:
        backoff = MIN_BACKOFF_SECONDS
        while not self._stopped.is_set():
            if self._attempt("reconcile", self.reconcile_once):
                backoff, delay = MIN_BACKOFF_SECONDS, float(RECONCILE_SECONDS)
            else:
                delay, backoff = backoff, min(backoff * 2, MAX_BACKOFF_SECONDS)
            self._sleep_with_jitter(delay)

    def _control_loop(self) -> None:
        backoff = MIN_BACKOFF_SECONDS
        while not self._stopped.is_set():
            if self._attempt("control poll", self._poll_once):
                backoff = MIN_BACKOFF_SECONDS
            else:
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _poll_once(self) -> None:
        command = self._control.poll()
        if command is not None:
            self.handle_command(command)

    def _attempt(self, name: str, step: Callable[[], None]) -> bool:
        """Runs one loop step. False means back off; nothing but a refused key ends the loop."""
        try:
            step()
            return True
        except ControlPlaneAuthError as error:
            logger.critical("the control plane refused the worker key (%s); stopping", error)
            self.fatal = error
            self._stopped.set()
        except ControlPlaneUnreachable as error:
            logger.warning("%s: control plane unreachable (%s)", name, error)
        except ControlPlaneRejected as error:
            logger.error("%s: the control plane refused the request (%s)", name, error)
        except Exception:
            logger.exception("%s failed unexpectedly", name)
        return False

    def _sleep_with_jitter(self, seconds: float) -> None:
        self._stopped.wait(seconds * (0.7 + random.random() * 0.6))  # noqa: S311


def _error(category: str, code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"category": category, "code": code, "messageText": message}}
