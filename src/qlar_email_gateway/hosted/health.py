"""A one-line HTTP answer for hosts that refuse a container which listens on nothing.

Azure App Service for Containers pings the container over HTTP when it starts and kills it if
nothing answers. The worker has no web surface of its own, so this serves `200 ok` and nothing
else: no mailbox, version or state is ever reported.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("qlar_email_gateway.hosted")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._answer(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._answer(send_body=False)

    def _answer(self, *, send_body: bool) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002, ANN002
        """Probes arrive every few seconds; they are not worth a log line."""


def start_health_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:  # noqa: S104
    """Answer `200 ok` to every GET on `port` from a daemon thread. Call `shutdown()` to stop."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("health listener on port %s", server.server_address[1])
    return server


def configured_port(environ: dict[str, str]) -> int | None:
    """The port the host asked us to answer on, or None when we are not on such a host.

    `WEBSITES_PORT` is what App Service documents for a container; `PORT` is what it also sets.
    Neither is set on a laptop or in plain Docker, so nothing listens there.
    """
    for name in ("WEBSITES_PORT", "PORT"):
        raw = environ.get(name, "").strip()
        if raw.isdigit() and 0 < int(raw) < 65536:
            return int(raw)
    return None
