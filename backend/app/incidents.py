"""Incident creation helpers.

The seeded demo creates incidents in bulk. Real sandbox payments arrive one at
a time, so they need a small, explicit entry point that links an incident to an
already-captured payment without touching the payment, order, or ledger rows
that already exist.

This module only creates the incident and its opening ledger entry. It never
moves money. The existing agent/policy path decides what happens next.
"""
from __future__ import annotations

import uuid
from typing import Any

from . import db


class IncidentError(Exception):
    """An incident-creation failure with the HTTP status the API should return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _existing_open_refund_incident(payment_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM incidents WHERE payment_id = ? AND kind = 'refund_request' "
        "AND status IN ('open', 'awaiting_approval') "
        "ORDER BY created_at DESC LIMIT 1",
        (payment_id,),
    )


def _payload(
    incident: dict[str, Any],
    payment: dict[str, Any],
    order: dict[str, Any],
    *,
    created: bool,
) -> dict[str, Any]:
    return {
        "incident_id": incident["id"],
        "created": created,
        "kind": incident["kind"],
        "status": incident["status"],
        "payment_id": payment["id"],
        "order_id": order["id"],
        "merchant_id": incident["merchant_id"],
        "customer_id": order["customer_id"],
        "amount_cents": payment["amount_cents"],
        "currency": payment["currency"],
        "paypal_order_id": payment["paypal_order_id"] or order["paypal_order_id"],
        "paypal_capture_id": payment["paypal_capture_id"],
    }


def create_refund_incident(payment_id: str) -> dict[str, Any]:
    """Create (or return) an open refund_request incident for a captured payment.

    Idempotent per payment: while an open/awaiting_approval refund_request
    incident already exists, repeat calls return that incident instead of
    creating a second one. Nothing is written until every relationship and the
    captured state have been validated.
    """
    payment = db.query_one("SELECT * FROM payments WHERE id = ?", (payment_id,))
    if not payment:
        raise IncidentError("payment not found", 404)

    order = db.query_one("SELECT * FROM orders WHERE id = ?", (payment["order_id"],))
    if not order:
        raise IncidentError("order not found for payment", 409)

    customer = db.query_one(
        "SELECT id, merchant_id FROM customers WHERE id = ?",
        (order["customer_id"],),
    )
    if not customer:
        raise IncidentError("customer not found for order", 409)

    merchant = db.query_one(
        "SELECT id FROM merchants WHERE id = ?", (order["merchant_id"],)
    )
    if not merchant:
        raise IncidentError("merchant not found for order", 409)

    if customer["merchant_id"] != order["merchant_id"]:
        raise IncidentError("customer and order belong to different merchants", 409)
    if payment["currency"] != order["currency"]:
        raise IncidentError("payment and order currencies do not match", 409)
    if payment["status"] != "succeeded":
        raise IncidentError(
            f"cannot request a refund for payment status {payment['status']!r}", 409
        )
    if not payment["paypal_capture_id"]:
        raise IncidentError(
            "payment has no PayPal capture id; a live refund would be unsafe", 409
        )
    if int(payment["amount_cents"]) <= 0:
        raise IncidentError("payment amount must be positive", 409)

    existing = _existing_open_refund_incident(payment_id)
    if existing:
        return _payload(existing, payment, order, created=False)

    incident_id = f"INC-SBX-{uuid.uuid4().hex[:10].upper()}"
    db.execute(
        "INSERT INTO incidents (id, merchant_id, order_id, payment_id, kind, "
        "status, created_at) VALUES (?, ?, ?, ?, 'refund_request', 'open', ?)",
        (incident_id, order["merchant_id"], order["id"], payment["id"], db.utcnow()),
    )
    db.append_ledger(
        "incident_opened",
        {
            "incident_id": incident_id,
            "kind": "refund_request",
            "payment_id": payment["id"],
            "order_id": order["id"],
            "amount_cents": payment["amount_cents"],
            "currency": payment["currency"],
        },
        incident_id,
    )

    incident = db.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    return _payload(incident, payment, order, created=True)
