"""Tests for the real-sandbox refund-request incident flow.

Every PayPal call here is mocked with a fake httpx response. Nothing touches the
network, the real database, or the real sandbox payment.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.agent.loop as loop
import app.payment_client as payment_client
from app import db, incidents
from app.agent.tools import ToolError, ToolRegistry
from app.main import app
from app.payment_client import SANDBOX_API_BASE, PayPilotClient

ORDER_ID = "O-SBX-69ADF503DD"
PAYMENT_ID = "P-SBX-F87936E810"
PAYPAL_ORDER_ID = "43664620KL771493V"
CAPTURE_ID = "28623564UA658335F"


def _add_captured_payment(
    *,
    payment_id: str = PAYMENT_ID,
    status: str = "succeeded",
    capture_id: str | None = CAPTURE_ID,
) -> None:
    """Insert the captured sandbox payment into the isolated test DB."""
    db.execute(
        "INSERT INTO orders (id, merchant_id, customer_id, amount_cents, currency, "
        "created_at, status, paypal_order_id) VALUES (?, 'M-1', 'C-1', 1234, "
        "'USD', ?, 'fulfilled', ?)",
        (ORDER_ID, db.utcnow(), PAYPAL_ORDER_ID),
    )
    db.execute(
        "INSERT INTO payments (id, paypal_capture_id, paypal_order_id, order_id, "
        "amount_cents, currency, status, attempt_count, risk_score, created_at) "
        "VALUES (?, ?, ?, ?, 1234, 'USD', ?, 1, 0.0, ?)",
        (payment_id, capture_id, PAYPAL_ORDER_ID, ORDER_ID, status, db.utcnow()),
    )


class RecordingRefundClient:
    def __init__(self, *, live: bool = True, ok: bool = True):
        self.live = live
        self.ok = ok
        self.calls: list[dict] = []

    def refund(self, **kwargs):
        self.calls.append(kwargs)
        if self.ok:
            return {
                "ok": True,
                "mode": "live" if self.live else "mock",
                "id": "REF-1",
            }
        return {
            "ok": False,
            "mode": "live" if self.live else "mock",
            "error": {"name": "REFUND_FAILED", "message": "refund rejected"},
        }


def _live_client(monkeypatch) -> PayPilotClient:
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
    return client


def _run_with_client(monkeypatch, incident_id: str, client) -> dict:
    monkeypatch.setattr(
        loop,
        "ToolRegistry",
        lambda iid: ToolRegistry(iid, paypal=client),
    )
    return loop.run_incident(incident_id)


def test_api_creates_refund_incident_for_captured_payment(fresh):
    _add_captured_payment()

    with TestClient(app) as api:
        response = api.post(
            "/api/incidents/refund-request", json={"payment_id": PAYMENT_ID}
        )
        assert response.status_code == 201
        body = response.json()

        assert body["created"] is True
        assert body["incident_id"].startswith("INC-SBX-")
        assert body["payment_id"] == PAYMENT_ID
        assert body["order_id"] == ORDER_ID
        assert body["merchant_id"] == "M-1"
        assert body["customer_id"] == "C-1"
        assert body["amount_cents"] == 1234
        assert body["currency"] == "USD"
        assert body["paypal_order_id"] == PAYPAL_ORDER_ID
        assert body["paypal_capture_id"] == CAPTURE_ID

        incident = db.query_one(
            "SELECT * FROM incidents WHERE id = ?", (body["incident_id"],)
        )
        assert incident["kind"] == "refund_request"
        assert incident["status"] == "open"
        assert incident["payment_id"] == PAYMENT_ID
        assert incident["order_id"] == ORDER_ID
        assert incident["merchant_id"] == "M-1"

        ledger = db.query_one(
            "SELECT * FROM ledger WHERE incident_id = ? AND kind = 'incident_opened'",
            (body["incident_id"],),
        )
        assert ledger is not None

        # Repeat creation is idempotent while the incident is open.
        again = api.post(
            "/api/incidents/refund-request", json={"payment_id": PAYMENT_ID}
        )
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["incident_id"] == body["incident_id"]

    count = db.query_one(
        "SELECT COUNT(*) AS n FROM incidents WHERE payment_id = ? "
        "AND kind = 'refund_request'",
        (PAYMENT_ID,),
    )["n"]
    assert count == 1


def test_create_refuses_payment_without_capture_id(fresh):
    _add_captured_payment(capture_id=None)

    with pytest.raises(incidents.IncidentError) as exc:
        incidents.create_refund_incident(PAYMENT_ID)

    assert exc.value.status_code == 409
    assert "capture id" in exc.value.message
    assert db.query_one(
        "SELECT id FROM incidents WHERE payment_id = ?", (PAYMENT_ID,)
    ) is None


def test_policy_requires_human_approval_above_merchant_limit(fresh):
    _add_captured_payment()
    db.execute(
        "UPDATE merchants SET refund_auto_limit_cents = 1000 WHERE id = 'M-1'"
    )
    incident = incidents.create_refund_incident(PAYMENT_ID)
    fake = RecordingRefundClient(live=True, ok=True)
    reg = ToolRegistry(incident["incident_id"], paypal=fake)

    payment = reg.get_payment(PAYMENT_ID)
    risk = reg.check_risk()
    proposed = reg.propose_action(
        type="refund",
        params={"amount_cents": 1234, "reason": "customer refund request"},
        evidence_ids=[payment["evidence_id"], risk["evidence_id"]],
    )

    assert proposed["verdict"] == "review"
    assert proposed["status"] == "awaiting_approval"

    with pytest.raises(ToolError):
        reg.execute_action(proposed["action_id"])
    assert fake.calls == []

    approved = reg.execute_action(proposed["action_id"], human_approved=True)
    assert approved["status"] == "executed"
    assert fake.calls[0]["capture_id"] == CAPTURE_ID
    assert fake.calls[0]["amount_cents"] == 1234
    assert fake.calls[0]["currency"] == "USD"


def test_agent_refunds_captured_payment_with_mocked_sandbox_http(fresh, monkeypatch):
    _add_captured_payment()
    incident = incidents.create_refund_incident(PAYMENT_ID)
    client = _live_client(monkeypatch)
    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return httpx.Response(
            200,
            json={
                "id": "REFUND-1",
                "status": "COMPLETED",
                "amount": {"value": "12.34", "currency_code": "USD"},
            },
        )

    monkeypatch.setattr(payment_client.httpx, "post", fake_post)

    result = _run_with_client(monkeypatch, incident["incident_id"], client)

    assert result["actions"][0]["verdict"] == "allow"
    assert result["executed"]["status"] == "executed"
    assert result["executed"]["result"]["ok"] is True
    assert result["executed"]["result"]["mode"] == "live"
    assert len(posts) == 1

    url, kwargs = posts[0]
    assert url == (
        f"{SANDBOX_API_BASE}/v2/payments/captures/{CAPTURE_ID}/refund"
    )
    assert kwargs["json"]["amount"] == {
        "value": "12.34",
        "currency_code": "USD",
    }
    assert kwargs["json"]["note_to_payer"] == "customer refund request"

    payment = db.query_one("SELECT * FROM payments WHERE id = ?", (PAYMENT_ID,))
    order = db.query_one("SELECT * FROM orders WHERE id = ?", (ORDER_ID,))
    incident_row = db.query_one(
        "SELECT * FROM incidents WHERE id = ?", (incident["incident_id"],)
    )
    action = db.query_one(
        "SELECT * FROM actions WHERE incident_id = ?", (incident["incident_id"],)
    )

    assert payment["status"] == "refunded"
    assert payment["paypal_capture_id"] == CAPTURE_ID
    assert order["status"] == "refunded"
    assert incident_row["status"] == "resolved"
    assert incident_row["outcome"] == "refunded"
    assert action["status"] == "executed"

    # Re-running cannot refund twice: the payment is now refunded, so the
    # policy engine denies the new proposal and no second HTTP call is made.
    second = _run_with_client(monkeypatch, incident["incident_id"], client)
    assert second["executed"] is None
    assert second["actions"][0]["verdict"] == "deny"
    assert len(posts) == 1


def test_agent_refund_failure_leaves_money_state_unchanged(fresh, monkeypatch):
    _add_captured_payment()
    incident = incidents.create_refund_incident(PAYMENT_ID)
    client = _live_client(monkeypatch)

    monkeypatch.setattr(
        payment_client.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "message": "refund rejected",
                "debug_id": "debug-refund",
                "details": [
                    {
                        "issue": "REFUND_FAILED",
                        "description": "The refund could not be processed.",
                    }
                ],
            },
        ),
    )

    result = _run_with_client(monkeypatch, incident["incident_id"], client)

    assert result["executed"]["status"] == "failed"
    assert result["executed"]["result"]["ok"] is False
    assert (
        result["executed"]["result"]["error"]["details"][0]["issue"]
        == "REFUND_FAILED"
    )

    payment = db.query_one("SELECT * FROM payments WHERE id = ?", (PAYMENT_ID,))
    order = db.query_one("SELECT * FROM orders WHERE id = ?", (ORDER_ID,))
    incident_row = db.query_one(
        "SELECT * FROM incidents WHERE id = ?", (incident["incident_id"],)
    )
    action = db.query_one(
        "SELECT * FROM actions WHERE incident_id = ?", (incident["incident_id"],)
    )

    assert payment["status"] == "succeeded"
    assert order["status"] == "fulfilled"
    assert incident_row["status"] == "open"
    assert incident_row["outcome"] is None
    assert action["status"] == "failed"
