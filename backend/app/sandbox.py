"""Sandbox checkout flow.

Two steps, mirroring PayPal's own checkout model:

    create_test_order()  -> internal order/payment rows + a PayPal approval URL
    capture_test_order() -> verify the buyer approved, capture, then persist

Internal ids are generated here and never reused; PayPal's ids live in the
existing external-id columns. Money is only marked succeeded after PayPal's
capture response explicitly reports a COMPLETED capture.
"""
from __future__ import annotations

import uuid
from typing import Any

from . import db
from .config import DEFAULT_CHECKOUT_CURRENCY
from .payment_client import PayPilotClient


class SandboxError(Exception):
    """A checkout-flow failure with the HTTP status the API should return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _internal_id(prefix: str) -> str:
    return f"{prefix}-SBX-{uuid.uuid4().hex[:10].upper()}"


def _safe_error(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        parts = [
            str(error[key])
            for key in ("name", "message", "debug_id")
            if error.get(key)
        ]
        for detail in error.get("details") or []:
            if not isinstance(detail, dict):
                continue
            issue = detail.get("issue")
            description = detail.get("description")
            if issue and description:
                parts.append(f"{issue}: {description}")
            elif issue:
                parts.append(str(issue))
            elif description:
                parts.append(str(description))
        if parts:
            return "PayPal: " + " | ".join(parts)
        if error.get("http_status"):
            return f"PayPal request failed (HTTP {error['http_status']})"
    return "PayPal request failed"


def _approval_url(result: dict[str, Any]) -> str | None:
    for link in result.get("links") or []:
        if link.get("rel") in {"payer-action", "approve"}:
            return link.get("href")
    return None


def _completed_capture_id(result: dict[str, Any]) -> str | None:
    for unit in result.get("purchase_units") or []:
        captures = (unit.get("payments") or {}).get("captures") or []
        for capture in captures:
            if capture.get("status") == "COMPLETED" and capture.get("id"):
                return capture["id"]
    return None


def create_test_order(
    amount_cents: int,
    currency: str = DEFAULT_CHECKOUT_CURRENCY,
    merchant_id: str = "M-1",
    customer_id: str = "C-1",
    client: PayPilotClient | None = None,
) -> dict[str, Any]:
    """Create an internal order/payment pair and a real sandbox order."""
    if amount_cents <= 0:
        raise SandboxError("amount_cents must be positive", 400)
    currency = currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        raise SandboxError("currency must be a three-letter ISO code", 400)

    if not db.query_one("SELECT id FROM merchants WHERE id = ?", (merchant_id,)):
        raise SandboxError("merchant not found", 404)
    customer = db.query_one(
        "SELECT id, merchant_id FROM customers WHERE id = ?", (customer_id,)
    )
    if not customer:
        raise SandboxError("customer not found", 404)
    if customer["merchant_id"] != merchant_id:
        raise SandboxError("customer does not belong to merchant", 400)

    order_id = _internal_id("O")
    payment_id = _internal_id("P")
    created_at = db.utcnow()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO orders (id, merchant_id, customer_id, amount_cents, "
            "currency, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'created')",
            (order_id, merchant_id, customer_id, amount_cents, currency, created_at),
        )
        conn.execute(
            "INSERT INTO payments (id, order_id, amount_cents, currency, status, "
            "attempt_count, created_at) VALUES (?, ?, ?, ?, 'pending', 1, ?)",
            (payment_id, order_id, amount_cents, currency, created_at),
        )

    client = client or PayPilotClient()
    try:
        result = client.create_order(
            amount_cents,
            currency,
            request_id=f"paypilot-create-{order_id}",
            reference_id=order_id,
        )
    except Exception as exc:  # network failure must not mark money moved
        db.execute(
            "UPDATE payments SET failure_reason = ? WHERE id = ?",
            ("PayPal order creation failed", payment_id),
        )
        raise SandboxError("PayPal order creation failed", 502) from exc

    if not result.get("ok"):
        reason = _safe_error(result)
        db.execute(
            "UPDATE payments SET failure_reason = ? WHERE id = ?", (reason, payment_id)
        )
        raise SandboxError(reason, 502)

    paypal_order_id = result.get("id")
    if not paypal_order_id:
        raise SandboxError("PayPal did not return an order id", 502)

    db.execute(
        "UPDATE orders SET paypal_order_id = ? WHERE id = ?",
        (paypal_order_id, order_id),
    )
    db.execute(
        "UPDATE payments SET paypal_order_id = ? WHERE id = ?",
        (paypal_order_id, payment_id),
    )

    approval_url = _approval_url(result)
    if not approval_url:
        raise SandboxError("PayPal did not return an approval URL", 502)
    db.append_ledger(
        "sandbox_order_created",
        {
            "order_id": order_id,
            "payment_id": payment_id,
            "paypal_order_id": paypal_order_id,
            "amount_cents": amount_cents,
            "currency": currency,
        },
    )

    return {
        "order_id": order_id,
        "payment_id": payment_id,
        "paypal_order_id": paypal_order_id,
        "status": result.get("status", "CREATED"),
        "approval_url": approval_url,
        "amount_cents": amount_cents,
        "currency": currency,
        "mode": result.get("mode"),
    }


def capture_test_order(
    order_id: str, client: PayPilotClient | None = None
) -> dict[str, Any]:
    """Capture only after PayPal says APPROVED, and only persist on COMPLETED."""
    order = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        raise SandboxError("order not found", 404)
    payment = db.query_one(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY created_at LIMIT 1",
        (order_id,),
    )
    if not payment:
        raise SandboxError("payment not found for order", 409)

    # Safe retry: the first successful capture is already persisted.
    if payment["status"] == "succeeded" and payment["paypal_capture_id"]:
        return {
            "order_id": order_id,
            "payment_id": payment["id"],
            "paypal_order_id": order["paypal_order_id"] or payment["paypal_order_id"],
            "paypal_capture_id": payment["paypal_capture_id"],
            "status": "succeeded",
            "amount_cents": order["amount_cents"],
            "currency": order["currency"],
            "mode": "already_captured",
        }

    paypal_order_id = order["paypal_order_id"] or payment["paypal_order_id"]
    if not paypal_order_id:
        raise SandboxError(
            "order has no PayPal order id; create it before capturing", 409
        )

    client = client or PayPilotClient()
    try:
        current = client.get_order(paypal_order_id)
    except Exception as exc:
        raise SandboxError("could not read PayPal order", 502) from exc
    if not current.get("ok"):
        raise SandboxError(_safe_error(current), 502)

    state = current.get("status")
    if state != "APPROVED":
        raise SandboxError(
            f"PayPal order is {state!r}; buyer approval is required before capture",
            409,
        )

    try:
        result = client.capture_order(
            paypal_order_id, request_id=f"paypilot-capture-{order_id}"
        )
    except Exception as exc:
        raise SandboxError("PayPal capture request failed", 502) from exc
    if not result.get("ok"):
        raise SandboxError(_safe_error(result), 502)

    capture_id = _completed_capture_id(result)
    if result.get("status") != "COMPLETED" or not capture_id:
        raise SandboxError(
            "PayPal did not confirm a completed capture; payment left unchanged",
            502,
        )

    db.execute(
        "UPDATE payments SET paypal_order_id = ?, paypal_capture_id = ?, "
        "status = 'succeeded', failure_reason = NULL WHERE id = ?",
        (paypal_order_id, capture_id, payment["id"]),
    )
    db.execute(
        "UPDATE orders SET paypal_order_id = ?, status = 'fulfilled' WHERE id = ?",
        (paypal_order_id, order_id),
    )
    db.append_ledger(
        "sandbox_order_captured",
        {
            "order_id": order_id,
            "payment_id": payment["id"],
            "paypal_order_id": paypal_order_id,
            "paypal_capture_id": capture_id,
        },
    )

    return {
        "order_id": order_id,
        "payment_id": payment["id"],
        "paypal_order_id": paypal_order_id,
        "paypal_capture_id": capture_id,
        "status": "succeeded",
        "amount_cents": order["amount_cents"],
        "currency": order["currency"],
        "mode": result.get("mode"),
    }
