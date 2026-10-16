import http.client

import pytest

from qlar_email_gateway.hosted.health import configured_port, start_health_server


@pytest.fixture
def server():
    started = start_health_server(0, host="127.0.0.1")
    yield started
    started.shutdown()
    started.server_close()


def _request(server, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.read(), response.getheader("Cache-Control")
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["/", "/healthz", "/anything/else?x=1"])
def test_every_get_is_answered_ok_and_nothing_else(server, path):
    status, body, cache = _request(server, "GET", path)

    assert (status, body, cache) == (200, b"ok", "no-store")


def test_head_is_answered_without_a_body(server):
    status, body, _ = _request(server, "HEAD", "/")

    assert (status, body) == (200, b"")


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, None),
        ({"PORT": "80"}, 80),
        ({"WEBSITES_PORT": "8080"}, 8080),
        ({"WEBSITES_PORT": "8080", "PORT": "80"}, 8080),
        ({"PORT": "abc"}, None),
        ({"PORT": "0"}, None),
        ({"PORT": "70000"}, None),
        ({"WEBSITES_PORT": "", "PORT": "80"}, 80),
    ],
)
def test_configured_port(environ, expected):
    assert configured_port(environ) == expected
