"""Refund regression tests.

The seeded demo uses internal payment ids (P-1004). PayPal only understands a
real capture id, so live refunds must refuse to call PayPal without one. These
tests also pin the failure behavior: a rejected refund changes no money state
and never escapes as an uncaught endpoint exception.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import app.agent.loop as loop
import app.payment_client as payment_client
from app import db
from app.agent.tools import ToolRegistry
from app.main import app
from app.payment_client import SANDBOX_API_BASE, PayPilotClient


class RecordingRefundClient:
    def __init__(self, *, live: bool, ok: bool = True):
        self.live = live
        self.ok = ok
        self.calls: list[dict] = []

    def refund(self, **kwargs):
        self.calls.append(kwargs)
        if self.ok:
            return {
                "ok": True,
                "mode": "live" if self.live else "mock",
                "refund_id": "REF-1",
                "capture_id": kwargs["capture_id"],
            }
        return {
            "ok": False,
            "mode": "live" if self.live else "mock",
            "error": {
                "name": "UNPROCESSABLE_ENTITY",
                "message": "refund rejected",
                "debug_id": "debug-refund",
            },
        }


def _propose_duplicate_refund(reg: ToolRegistry) -> dict:
    payment = reg.get_payment("P-1004")
    risk = reg.check_risk()
    return reg.propose_action(
        type="refund",
        params={"amount_cents": 2500, "reason": "confirmed duplicate charge"},
        evidence_ids=[payment["evidence_id"], risk["evidence_id"]],
    )


def _payment_and_order():
    return (
        db.query_one("SELECT * FROM payments WHERE id = 'P-1004'"),
        db.query_one("SELECT * FROM orders WHERE id = 'O-1002'"),
    )


def test_mock_refund_still_succeeds(fresh):
    fake = RecordingRefundClient(live=False, ok=True)
    reg = ToolRegistry("INC-004", paypal=fake)

    proposed = _propose_duplicate_refund(reg)
    assert proposed["verdict"] == "allow"

    result = reg.execute_action(proposed["action_id"])

    assert result["status"] == "executed"
    assert result["outcome"] == "refunded"
    # Mock mode may use the internal id; no PayPal network call exists.
    assert fake.calls[0]["capture_id"] == "P-1004"
    payment, order = _payment_and_order()
    assert payment["status"] == "refunded"
    assert order["status"] == "refunded"


def test_live_refund_without_capture_id_never_calls_paypal(fresh):
    fake = RecordingRefundClient(live=True, ok=True)
    reg = ToolRegistry("INC-004", paypal=fake)

    proposed = _propose_duplicate_refund(reg)
    result = reg.execute_action(proposed["action_id"])

    assert result["status"] == "failed"
    assert result["outcome"] is None
    assert fake.calls == []

    payment, order = _payment_and_order()
    assert payment["status"] == "succeeded"
    assert payment["paypal_capture_id"] is None
    assert order["status"] == "fulfilled"

    action = db.query_one("SELECT * FROM actions WHERE id = ?",
                          (proposed["action_id"],))
    assert action["status"] == "failed"
    assert '"MISSING_CAPTURE_ID"' in action["result"]

    incident = db.query_one("SELECT * FROM incidents WHERE id = 'INC-004'")
    assert incident["status"] == "open"
    assert incident["outcome"] is None


def test_live_refund_uses_stored_capture_id(fresh):
    db.execute(
        "UPDATE payments SET paypal_capture_id = 'CAP-REAL' WHERE id = 'P-1004'"
    )
    fake = RecordingRefundClient(live=True, ok=True)
    reg = ToolRegistry("INC-004", paypal=fake)

    proposed = _propose_duplicate_refund(reg)
    result = reg.execute_action(proposed["action_id"])

    assert result["status"] == "executed"
    assert result["outcome"] == "refunded"
    assert fake.calls[0]["capture_id"] == "CAP-REAL"
    assert fake.calls[0]["capture_id"] != "P-1004"

    payment, order = _payment_and_order()
    assert payment["status"] == "refunded"
    assert order["status"] == "refunded"


def test_live_refund_failure_does_not_mark_refunded(fresh):
    db.execute(
        "UPDATE payments SET paypal_capture_id = 'CAP-REAL' WHERE id = 'P-1004'"
    )
    fake = RecordingRefundClient(live=True, ok=False)
    reg = ToolRegistry("INC-004", paypal=fake)

    proposed = _propose_duplicate_refund(reg)
    result = reg.execute_action(proposed["action_id"])

    assert result["status"] == "failed"
    assert result["outcome"] is None
    assert fake.calls[0]["capture_id"] == "CAP-REAL"

    payment, order = _payment_and_order()
    assert payment["status"] == "succeeded"
    assert order["status"] == "fulfilled"

    action = db.query_one("SELECT * FROM actions WHERE id = ?",
                          (proposed["action_id"],))
    assert action["status"] == "failed"
    assert '"ok": false' in action["result"]


def test_adapter_refund_returns_error_instead_of_raising(monkeypatch):
    monkeypatch.setattr(
        payment_client,
        "settings",
        SimpleNamespace(
            paypal_live=True,
            paypal_api_base=SANDBOX_API_BASE,
            paypal_client_id="test-id",
            paypal_client_secret="test-secret",
        ),
    )
    client = PayPilotClient()
    monkeypatch.setattr(client, "_access_token", lambda: "fake-token")

    monkeypatch.setattr(
        payment_client.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            404,
            json={
                "name": "RESOURCE_NOT_FOUND",
                "message": "capture not found",
                "debug_id": "debug-404",
            },
        ),
    )

    result = client.refund("CAP-MISSING", 100)

    assert result["ok"] is False
    assert result["error"]["http_status"] == 404
    assert result["error"]["name"] == "RESOURCE_NOT_FOUND"
    assert "fake-token" not in str(result)


def test_adapter_mock_refund_is_still_available():
    client = PayPilotClient()
    assert client.live is False

    result = client.refund("P-1004", 2500)

    assert result["ok"] is True
    assert result["mode"] == "mock"


def test_incident_run_endpoint_does_not_500_on_refund_failure(fresh, monkeypatch):
    db.execute(
        "UPDATE payments SET paypal_capture_id = 'CAP-REAL' WHERE id = 'P-1004'"
    )
    fake = RecordingRefundClient(live=True, ok=False)
    real_registry = loop.ToolRegistry
    monkeypatch.setattr(
        loop,
        "ToolRegistry",
        lambda incident_id: real_registry(incident_id, paypal=fake),
    )

    with TestClient(app) as api:
        response = api.post("/api/incidents/INC-004/run")

    assert response.status_code == 200
    body = response.json()
    assert body["executed"]["status"] == "failed"

    payment, _ = _payment_and_order()
    assert payment["status"] == "succeeded"
