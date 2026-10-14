"""The hosted worker's composition root: which mailboxes run, and what the control plane asks of them.

Two threads of its own. The reconcile loop compares Qlar's list of hosted registrations with the
runtimes it holds, every `RECONCILE_SECONDS`; the control loop long-polls for commands that should
not wait for the next reconcile (a new mailbox, new credentials, a credentials test).
"""

from __future__ import annotations

import json
import logging
import random
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
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

REVOKED = "revoked"


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
    ) -> None:
        self._control = control
        self._vault_uri = vault_uri
        self._base_url = base_url
        self._state_dir = state_dir
        self._fetch_credentials = fetch_credentials
        self._runtime_factory = runtime_factory
        self._mailbox_tester = mailbox_tester
        self._executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS, thread_name_prefix="qlar-job")
        self.runtimes: dict[str, Any] = {}
        #: Mailboxes whose runtime Qlar stopped (deleted/revoked) while they were still listed:
        #: left alone until they leave the list, so a disagreement never becomes a re-enrol loop.
        self._halted: set[str] = set()
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

        with self._lock:
            for gateway_id in list(self.runtimes):
                self._drop(gateway_id)
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self.fatal is not None:
            raise self.fatal

    def stop(self) -> None:
        self._stopped.set()

    # -- reconcile -----------------------------------------------------------------------------

    def reconcile_once(self) -> None:
        rows = self._control.list_registrations()

        with self._lock:
            desired = self._desired(rows)

            for gateway_id in [g for g in self.runtimes if g not in desired]:
                logger.info("mailbox %s is gone or revoked; stopping it", gateway_id)
                self._drop(gateway_id)
            self._halted &= desired.keys()

            for gateway_id, runtime in list(self.runtimes.items()):
                if runtime.is_alive:
                    continue
                reason = runtime.stopped_by
                self._drop(gateway_id)
                if reason:
                    logger.error("mailbox %s was stopped by Qlar (%s); not restarting it", gateway_id, reason)
                    self._halted.add(gateway_id)
                else:
                    logger.warning("mailbox %s stopped unexpectedly; restarting it", gateway_id)

            for gateway_id, registration in desired.items():
                if gateway_id not in self.runtimes and gateway_id not in self._halted:
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
        gateway_id = str(registration["gatewayId"])
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

        runtime = self._runtime_factory(
            registration, mail, self._base_url, self._state_dir, self._control, self._executor
        )
        try:
            runtime.ensure_enrolled()
            runtime.start()
        except Exception as error:  # noqa: BLE001 - one mailbox must not take the others down
            logger.error(
                "skipping mailbox %s: could not start (%s: %s)", gateway_id, type(error).__name__, error
            )
            runtime.stop()
            return False

        self.runtimes[gateway_id] = runtime
        logger.info("mailbox %s started", gateway_id)
        return True

    def _drop(self, gateway_id: str) -> None:
        runtime = self.runtimes.pop(gateway_id, None)
        if runtime is not None:
            runtime.stop()

    # -- commands ------------------------------------------------------------------------------

    def handle_command(self, cmd: dict[str, Any]) -> None:
        kind = str(cmd.get("type", ""))
        gateway_id = str(cmd.get("gatewayId") or "")

        if kind in ("configure", "refresh_credentials"):
            self._restart(gateway_id)
        elif kind == "remove":
            with self._lock:
                self._drop(gateway_id)
                self._halted.discard(gateway_id)
        elif kind == "test_credentials":
            self._executor.submit(self._test_credentials, cmd)
        else:
            logger.warning("ignoring unknown control command %r", kind)

    def _restart(self, gateway_id: str) -> None:
        registration = next(
            (r for r in self._control.list_registrations() if str(r.get("gatewayId")) == gateway_id),
            None,
        )
        with self._lock:
            self._drop(gateway_id)
            self._halted.discard(gateway_id)
            if registration is None or str(registration.get("status", "")).lower() == REVOKED:
                logger.info("nothing to start for mailbox %s (gone or revoked)", gateway_id)
                return
            if len(self.runtimes) >= MAX_MAILBOXES:
                logger.error(
                    "not starting mailbox %s: the worker already serves %d", gateway_id, MAX_MAILBOXES
                )
                return
            self._start(registration)

    def _test_credentials(self, cmd: dict[str, Any]) -> None:
        control_id = str(cmd.get("id") or cmd.get("controlId") or "")
        started = time.monotonic()
        tester = self._mailbox_tester or mailbox_module.test_mailbox
        try:
            mail = parse_mail_settings(json.dumps(cmd.get("credentials")))
            settings = build_settings("credentials-test", self._base_url, self._state_dir, mail, None)
            report = tester(settings)
            payload: dict[str, Any] = {
                "status": "ok",
                "durationMs": int((time.monotonic() - started) * 1000),
                **report,
            }
        except CredentialFormatError as error:
            payload = _error("rejected", "invalid_credentials", str(error))
        except (mailbox_module.MailboxError, SmtpSendError) as error:
            payload = _error(error.category, str(getattr(error, "code", "")), str(error))
        except Exception as error:  # noqa: BLE001 - the waiting caller must always get an answer
            logger.exception("credentials test %s failed unexpectedly", control_id)
            payload = _error("internal", type(error).__name__, "the worker failed unexpectedly")

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
