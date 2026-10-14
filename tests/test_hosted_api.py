import httpx
import pytest

from qlar_email_gateway.hosted.api import (
    ControlPlaneAuthError,
    ControlPlaneClient,
    ControlPlaneRejected,
    ControlPlaneUnreachable,
)


def make_client(handler) -> ControlPlaneClient:
    return ControlPlaneClient(
        "https://q/api/email-gateway", "k3y", transport=httpx.MockTransport(handler)
    )


def test_list_registrations_sends_key_and_returns_array():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["X-Internal-Api-Key"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"registrations": [{"gatewayId": "egw_1"}]})

    result = make_client(handler).list_registrations()

    assert seen == {"key": "k3y", "url": "https://q/api/email-gateway/hosted/registrations"}
    assert result == [{"gatewayId": "egw_1"}]


def test_poll_204_is_none():
    client = make_client(lambda request: httpx.Response(204))

    assert client.poll() is None


def test_poll_returns_command_object():
    command = {"id": "c1", "type": "test_credentials", "credentials": {"imapHost": "h"}}
    client = make_client(lambda request: httpx.Response(200, json={"command": command}))

    assert client.poll() == command


def test_403_raises_auth_error():
    client = make_client(lambda request: httpx.Response(403, json={"message": "no"}))

    with pytest.raises(ControlPlaneAuthError):
        client.poll()


def test_401_raises_auth_error():
    client = make_client(lambda request: httpx.Response(401))

    with pytest.raises(ControlPlaneAuthError):
        client.list_registrations()


def test_post_result_posts_json_to_control_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"accepted": True})

    make_client(handler).post_result("c1", {"ok": True})

    assert seen["url"].endswith("/hosted/control/c1/result")
    assert b'"ok"' in seen["body"]


def test_rekey_returns_enrollment_code():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"enrollmentCode": "CODE"})

    assert make_client(handler).rekey("egw_1") == "CODE"
    assert seen["url"].endswith("/hosted/registrations/egw_1/rekey")


def test_connection_error_raises_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ControlPlaneUnreachable):
        make_client(handler).poll()


def test_other_client_errors_raise_rejected_not_raw_httpx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "gateway not found"})

    with pytest.raises(ControlPlaneRejected) as caught:
        make_client(handler).rekey("egw_x")

    assert caught.value.status == 404
