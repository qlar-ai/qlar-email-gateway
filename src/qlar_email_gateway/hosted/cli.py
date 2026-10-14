"""`qlar-email-hosted-worker`: the Qlar-run process that serves every hosted mailbox.

Configured by five environment variables and nothing else; there is no terminal to prompt on and
no `.env` to write.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from .api import ControlPlaneClient
from .controller import HostedController

REQUIRED = ("QLAR_BASE_URL", "QLAR_WORKER_API_KEY", "KEY_VAULT_URI")
DEFAULT_STATE_DIR = "/state"


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        print(
            "missing required environment: " + ", ".join(missing) + ". Set "
            "QLAR_BASE_URL (ends in /api/email-gateway), QLAR_WORKER_API_KEY and KEY_VAULT_URI.",
            file=sys.stderr,
        )
        return 1

    base_url = os.environ["QLAR_BASE_URL"].strip().rstrip("/")
    if not base_url.startswith("https://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        print("QLAR_BASE_URL must use https:// outside local testing", file=sys.stderr)
        return 1

    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    log = logging.getLogger("qlar_email_gateway.hosted")

    control = ControlPlaneClient(base_url, os.environ["QLAR_WORKER_API_KEY"])
    controller = HostedController(
        control,
        vault_uri=os.environ["KEY_VAULT_URI"].strip(),
        base_url=base_url,
        state_dir=Path(os.environ.get("STATE_DIR", "").strip() or DEFAULT_STATE_DIR),
    )

    def handle_signal(signum, _frame):  # noqa: ANN001 - signal handler signature
        log.info("received signal %s, stopping", signum)
        controller.stop()

    # Once, for the whole process: every mailbox runtime shares this one handler.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_signal)

    try:
        controller.run_forever()
    except Exception:
        log.exception("the hosted worker crashed")
        return 1
    finally:
        control.close()
    return 0


def _configure_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=resolved, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if resolved > logging.DEBUG:
        for library in ("httpx", "httpcore", "azure"):
            logging.getLogger(library).setLevel(logging.WARNING)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
