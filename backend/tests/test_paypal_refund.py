"""Read-only PayPal refund lookup: payload shape, guards, and no writes.

No test here touches the network: the suite runs with ``PAYPAL_MODE=mock`` so
``PayPilotClient.get_refund`` returns its deterministic payload. The endpoint's
DB guard is exercised against the seeded database plus one executed refund row.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

import app.main as main
from app import db
from app.main import app

REFUND_ID = "3CE70701VR019145F"


def _record_executed_refund(refund_id: str = REFUND_ID) -> None:
    """Store a refund action whose result carries the PayPal refund id."""
    db.execute(
        "INSERT INTO actions (id, incident_id, type, params, evidence_ids, "
        "policy_verdict, policy_reason, status, executed_at, result) "
        "VALUES (?, ?, 'refund', ?, ?, 'allow', 'fixture', 'executed', ?, ?)",
        (
            "A-fixture-refund",
            "INC-003",
            json.dumps({"capture_id": "28623564UA658335F"}),
            json.dumps(["EV-1"]),
            db.utcnow(),
            json.dumps({"ok": True, "id": refund_id, "status": "COMPLETED"}),
        ),
    )


def _counts() -> dict[str, int]:
    return {
        table: db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        for table in ("evidence", "actions", "ledger", "refunds")
    }


def test_refund_lookup_returns_read_only_payload(fresh):
    _record_executed_refund()
    response = TestClient(app).get(f"/api/paypal/refunds/{REFUND_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == REFUND_ID
    assert body["status"] == "COMPLETED"
    assert body["amount"]["currency_code"] == "USD"
    assert body["amount"]["value"]
    assert body["source"] == "mock"
    assert isinstance(body["elapsed_ms"], int) and body["elapsed_ms"] >= 0
    assert "create_time" in body
    assert "note_to_payer" in body


def test_refund_lookup_rejects_unknown_or_malformed_id(fresh, monkeypatch):
    _record_executed_refund()

    def explode():  # the guard must fire before any PayPal client is built
        raise AssertionError("PayPal client must not be touched")

    monkeypatch.setattr(main, "_paypal_client", explode)
    client = TestClient(app)

    unknown = client.get("/api/paypal/refunds/ZZZZZZZZZZZZ")
    assert unknown.status_code == 404

    malformed = client.get("/api/paypal/refunds/abc")
    assert malformed.status_code == 404


def test_refund_lookup_writes_nothing(fresh):
    _record_executed_refund()
    before = _counts()

    response = TestClient(app).get(f"/api/paypal/refunds/{REFUND_ID}")
    assert response.status_code == 200

    assert _counts() == before
