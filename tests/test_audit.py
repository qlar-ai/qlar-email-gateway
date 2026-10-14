import json

from qlar_email_gateway.audit import AuditLog


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_gateway_id_recorded_when_given(tmp_path):
    path = tmp_path / "a.jsonl"

    AuditLog(path).record(status="forwarded", gateway_id="egw_1")

    assert read(path)[0]["gatewayId"] == "egw_1"


def test_gateway_id_omitted_when_not_given(tmp_path):
    path = tmp_path / "a.jsonl"

    AuditLog(path).record(status="forwarded")

    assert "gatewayId" not in read(path)[0]
