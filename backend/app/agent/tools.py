"""The tool registry — everything the agent is allowed to touch.

Design rule: read tools are cheap, reversible and always allowed. Action tools
are expensive, irreversible and go through the policy engine. The registry is
the complete surface area of the agent's power; if a capability is not a
function here, the agent cannot do it.

Every tool returns a dict. Read tools also record an evidence row and return its
id, which is what forces the agent to cite its reasoning.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from .. import db
from ..payment_client import PayPilotClient

# ---------------------------------------------------------------------------
# Merchant policy corpus. Simple keyword matching over a small in-code corpus.
# ---------------------------------------------------------------------------
POLICY_DOCS: dict[str, str] = {
    "refunds": (
        "Refund policy. Refunds up to the merchant's auto-refund limit may be "
        "issued automatically when the original payment succeeded. Refunds "
        "above that limit require human approval. A refund may never exceed "
        "the original payment amount, and no refund may be issued against a "
        "payment that did not succeed."
    ),
    "retries": (
        "Retry policy. A failed payment may be retried at most three times in "
        "total. Retries are prohibited when the payment already succeeded (this "
        "would double-charge) or when the risk score is at or above 0.85. When "
        "a card is declined for insufficient funds, contact the customer to "
        "confirm they want to try again before retrying."
    ),
    "fraud": (
        "Fraud policy. Any payment may be flagged for human review, but a flag "
        "must be backed by a risk score of at least 0.5. Scores at or above "
        "0.85 justify immediate automatic escalation. Suspicious-charge "
        "incidents are never closed automatically; a human resolves them."
    ),
    "disputes": (
        "Dispute policy. Duplicate charges are refunded as soon as the duplicate "
        "is confirmed from PayPal transaction data. Chargebacks are routed to "
        "the merchant's dispute team and are out of scope for automated action."
    ),
}


class ToolError(Exception):
    """Raised when a tool is asked for something that does not exist."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


class ToolRegistry:
    def __init__(self, incident_id: str, paypal: PayPilotClient | None = None):
        self.incident_id = incident_id
        self.paypal = paypal or PayPilotClient()
        self.calls: list[dict[str, Any]] = []

    # -- plumbing ----------------------------------------------------------
    def _evidence(self, tool: str, summary: str, payload: dict[str, Any]) -> str:
        count = len(
            db.query("SELECT id FROM evidence WHERE incident_id = ?", (self.incident_id,))
        )
        eid = f"{self.incident_id}-E{count + 1}"
        db.execute(
            "INSERT INTO evidence (id, incident_id, tool, summary, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, self.incident_id, tool, summary, _json(payload), db.utcnow()),
        )
        db.append_ledger(
            "tool_call",
            {"tool": tool, "evidence_id": eid, "summary": summary},
            self.incident_id,
        )
        self.calls.append({"tool": tool, "evidence_id": eid, "summary": summary})
        return eid

    def _ctx(self) -> dict[str, Any]:
        incident = db.query_one("SELECT * FROM incidents WHERE id = ?", (self.incident_id,))
        if not incident:
            raise ToolError(f"incident {self.incident_id} not found")
        payment = db.query_one(
            "SELECT * FROM payments WHERE id = ?", (incident["payment_id"],)
        )
        order = db.query_one("SELECT * FROM orders WHERE id = ?", (incident["order_id"],))
        customer = db.query_one(
            "SELECT * FROM customers WHERE id = ?", (order["customer_id"],)
        )
        merchant = db.query_one(
            "SELECT * FROM merchants WHERE id = ?", (incident["merchant_id"],)
        )
        return {
            "incident": incident,
            "payment": payment,
            "order": order,
            "customer": customer,
            "merchant": merchant,
        }

    # -- read tools --------------------------------------------------------
    def get_payment(self, payment_id: str) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM payments WHERE id = ?", (payment_id,))
        if not row:
            raise ToolError(f"payment {payment_id} not found")
        eid = self._evidence(
            "get_payment",
            f"Payment {payment_id}: {row['status']}, {row['amount_cents']} cents, "
            f"{row['attempt_count']} attempt(s)"
            + (f", reason {row['failure_reason']}" if row["failure_reason"] else ""),
            row,
        )
        return {"evidence_id": eid, "payment": row}

    def get_order(self, order_id: str) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not row:
            raise ToolError(f"order {order_id} not found")
        eid = self._evidence(
            "get_order",
            f"Order {order_id}: {row['status']}, {row['amount_cents']} cents created "
            f"{row['created_at']}",
            row,
        )
        return {"evidence_id": eid, "order": row}

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM customers WHERE id = ?", (customer_id,))
        if not row:
            raise ToolError(f"customer {customer_id} not found")
        known_devices = json.loads(row["known_device_ids"])
        eid = self._evidence(
            "get_customer",
            f"Customer {row['name']}: account since {row['account_created_at']}, "
            f"{row['lifetime_orders']} lifetime order(s), country {row['country_iso']}",
            {**row, "known_device_ids": known_devices},
        )
        return {"evidence_id": eid, "customer": row}

    def get_customer_history(self, customer_id: str) -> dict[str, Any]:
        rows = db.query(
            "SELECT p.id, p.amount_cents, p.status, p.failure_reason, p.created_at "
            "FROM payments p JOIN orders o ON o.id = p.order_id "
            "WHERE o.customer_id = ? ORDER BY p.created_at DESC",
            (customer_id,),
        )
        failed = sum(1 for r in rows if r["status"] == "failed")
        eid = self._evidence(
            "get_customer_history",
            f"{len(rows)} payment(s) on record, {failed} failed",
            {"payments": rows},
        )
        return {"evidence_id": eid, "payments": rows}

    def search_policy(self, query: str) -> dict[str, Any]:
        q = (query or "").lower()
        hits = {k: v for k, v in POLICY_DOCS.items() if k in q} or {
            k: v for k, v in POLICY_DOCS.items() if any(w in v.lower() for w in q.split())
        }
        if not hits:
            hits = POLICY_DOCS
        eid = self._evidence(
            "search_policy",
            f"Policy lookup for {query!r}: matched {', '.join(hits)}",
            hits,
        )
        return {"evidence_id": eid, "policies": hits}

    def check_risk(self) -> dict[str, Any]:
        """Deterministic risk score. Every point is attributable to a signal."""
        ctx = self._ctx()
        payment, customer = ctx["payment"], ctx["customer"]

        signals: list[dict[str, Any]] = []

        def add(name: str, weight: float, detail: str) -> None:
            signals.append({"signal": name, "weight": weight, "detail": detail})

        if payment["ip_country"] and payment["ip_country"] != customer["country_iso"]:
            add(
                "country_mismatch",
                0.25,
                f"IP country {payment['ip_country']} != customer country "
                f"{customer['country_iso']}",
            )

        account_age = datetime.fromisoformat(customer["account_created_at"])
        pay_time = datetime.fromisoformat(payment["created_at"])
        if (pay_time - account_age) < timedelta(days=30):
            add(
                "new_account",
                0.20,
                f"account was {(pay_time - account_age).days} day(s) old at payment time",
            )

        if int(payment["attempt_count"]) >= 2:
            add(
                "repeated_attempts",
                0.15,
                f"{payment['attempt_count']} attempts on one payment",
            )

        known = json.loads(customer["known_device_ids"])
        if payment["device_id"] and payment["device_id"] not in known:
            add(
                "unrecognised_device",
                0.20,
                f"device {payment['device_id']} has never been seen for this customer",
            )

        orders = int(customer["lifetime_orders"] or 0)
        if orders >= 1:
            avg = int(customer["lifetime_spend_cents"]) / orders
            if avg > 0 and int(payment["amount_cents"]) > 3 * avg:
                add(
                    "amount_outlier",
                    0.20,
                    f"{payment['amount_cents']} is more than 3x this customer's "
                    f"average of {avg:.0f}",
                )

        window_start = (pay_time - timedelta(minutes=10)).isoformat()
        recent = db.query(
            "SELECT COUNT(*) AS n FROM payments p JOIN orders o ON o.id = p.order_id "
            "WHERE o.customer_id = ? AND p.created_at BETWEEN ? AND ?",
            (customer["id"], window_start, payment["created_at"]),
        )[0]["n"]
        if recent >= 3:
            add("velocity", 0.15, f"{recent} payments within 10 minutes")

        signal_score = round(min(1.0, sum(s["weight"] for s in signals)), 3)
        recorded = float(payment["risk_score"] or 0.0)
        # The payment record may already carry a score from the processor. The
        # effective score is the stronger of that prior and our own signals, so
        # we never understate a known-suspicious payment.
        score = round(max(recorded, signal_score), 3)
        eid = self._evidence(
            "check_risk",
            f"risk score {score:.2f} (recorded {recorded:.2f}, "
            f"local signals {signal_score:.2f}) from {len(signals)} signal(s)"
            + (f": {', '.join(s['signal'] for s in signals)}" if signals else ""),
            {"score": score, "recorded_score": recorded,
             "signal_score": signal_score, "signals": signals},
        )
        return {"evidence_id": eid, "score": score, "signals": signals}

    # -- action tools ------------------------------------------------------
    def propose_action(
        self,
        type: str,
        params: dict[str, Any],
        evidence_ids: list[str],
    ) -> dict[str, Any]:
        """Record a proposal, run the policy engine, and persist the verdict."""
        from ..policy import evaluate

        ctx = self._ctx()
        risk_row = db.query_one(
            "SELECT payload FROM evidence WHERE incident_id = ? AND tool = 'check_risk' "
            "ORDER BY created_at DESC LIMIT 1",
            (self.incident_id,),
        )
        risk = json.loads(risk_row["payload"])["score"] if risk_row else 0.0

        result = evaluate(
            {"type": type, "params": params, "evidence_ids": evidence_ids},
            {**ctx, "risk_score": risk},
        )

        aid = f"A-{uuid.uuid4().hex[:8]}"
        status = "proposed" if result.executes else "awaiting_approval"
        if result.verdict == "deny":
            status = "rejected"

        db.execute(
            "INSERT INTO actions (id, incident_id, type, params, evidence_ids, "
            "policy_verdict, policy_reason, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, self.incident_id, type, _json(params), _json(evidence_ids),
             result.verdict, result.reason, status),
        )
        db.append_ledger(
            "action_proposed",
            {"action_id": aid, "type": type, "params": params,
             "evidence_ids": evidence_ids, "verdict": result.verdict,
             "reason": result.reason},
            self.incident_id,
        )
        return {
            "action_id": aid,
            "verdict": result.verdict,
            "reason": result.reason,
            "status": status,
        }

    def execute_action(self, action_id: str, human_approved: bool = False) -> dict[str, Any]:
        """Execute only actions the policy engine allowed — or that a human
        explicitly approved. No other path exists.

        `human_approved` is the one legitimate override: a person read the
        proposal and the evidence and took responsibility. It is recorded in
        the ledger with its own event kind so it can never be confused with an
        autonomous execution.
        """
        row = db.query_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not row:
            raise ToolError(f"action {action_id} not found")
        if row["policy_verdict"] != "allow" and not human_approved:
            raise ToolError(
                f"refusing to execute {action_id}: policy verdict was "
                f"{row['policy_verdict']!r} and no human approved it"
            )
        if row["policy_verdict"] == "deny" and human_approved:
            raise ToolError(
                f"action {action_id} was denied by policy; denial is absolute"
            )
        if row["status"] == "executed":
            return {"action_id": action_id, "status": "already_executed"}

        params = json.loads(row["params"])
        ctx = self._ctx()
        escalated = False
        action_status = "executed"

        if row["type"] == "refund":
            capture_id = ctx["payment"]["paypal_capture_id"]
            live = bool(getattr(self.paypal, "live", False))

            if live and not capture_id:
                # A seeded/internal id like P-1004 is not a PayPal capture id.
                # Refuse before making any network call.
                result = {
                    "ok": False,
                    "mode": "live",
                    "error": {
                        "name": "MISSING_CAPTURE_ID",
                        "message": "payment has no stored PayPal capture id; "
                                   "refusing to send an internal payment id",
                    },
                }
            else:
                try:
                    result = self.paypal.refund(
                        capture_id=capture_id or ctx["payment"]["id"],
                        amount_cents=int(params["amount_cents"]),
                        currency=ctx["payment"]["currency"],
                        note=params.get("reason", "PayPilot automated refund"),
                    )
                except Exception as exc:  # never let a refund crash the run
                    result = {
                        "ok": False,
                        "mode": "live" if live else "mock",
                        "error": {
                            "name": type(exc).__name__,
                            "message": "refund request failed",
                        },
                    }

            if result.get("ok"):
                db.execute("UPDATE payments SET status = 'refunded' WHERE id = ?",
                           (ctx["payment"]["id"],))
                db.execute("UPDATE orders SET status = 'refunded' WHERE id = ?",
                           (ctx["order"]["id"],))
                outcome = "refunded"
            else:
                # PayPal did not confirm the refund: change no money state.
                outcome = None
                action_status = "failed"
        elif row["type"] == "retry_payment":
            result = self.paypal.retry_order(
                order_id=ctx["payment"]["id"],
                amount_cents=int(ctx["payment"]["amount_cents"]),
                currency=ctx["payment"]["currency"],
            )
            db.execute(
                "UPDATE payments SET attempt_count = attempt_count + 1, status = ? "
                "WHERE id = ?",
                ("succeeded" if result.get("ok") else "failed",
                 ctx["payment"]["id"]),
            )
            outcome = "recovered" if result.get("ok") else "abandoned"
        elif row["type"] == "flag_for_review":
            db.execute("UPDATE incidents SET status = 'escalated' WHERE id = ?",
                       (self.incident_id,))
            result = {"ok": True, "note": "queued for human review"}
            outcome = "flagged"
            escalated = True
        elif row["type"] == "request_confirmation":
            result = {"ok": True, "note": "confirmation message queued to customer"}
            outcome = None
        elif row["type"] == "close_incident":
            result = {"ok": True}
            outcome = "confirmed_legit"
        else:  # pragma: no cover
            raise ToolError(f"no executor for action type {row['type']!r}")

        db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, result = ? "
            "WHERE id = ?",
            (action_status, db.utcnow(), _json(result), action_id),
        )
        if outcome:
            if escalated:
                # Escalation is a terminal-but-open state: record the outcome
                # without downgrading the incident to "resolved".
                db.execute(
                    "UPDATE incidents SET outcome = ? WHERE id = ?",
                    (outcome, self.incident_id),
                )
            else:
                db.execute(
                    "UPDATE incidents SET status = ?, outcome = ?, resolved_at = ? "
                    "WHERE id = ?",
                    ("resolved", outcome, db.utcnow(), self.incident_id),
                )
        db.append_ledger(
            "action_executed" if action_status == "executed" else "action_failed",
            {"action_id": action_id, "type": row["type"], "result": result,
             "outcome": outcome},
            self.incident_id,
        )
        return {"action_id": action_id, "status": action_status, "result": result,
                "outcome": outcome}

    # -- convenience -------------------------------------------------------
    def context(self) -> dict[str, Any]:
        c = self._ctx()
        return {
            "incident": c["incident"],
            "payment": c["payment"],
            "order": c["order"],
            "customer": c["customer"],
            "merchant": c["merchant"],
        }

    def describe_incident(self) -> str:
        c = self._ctx()
        return (
            f"Incident {self.incident_id} ({c['incident']['kind']}) for order "
            f"{c['order']['id']} from customer {c['customer']['name']} "
            f"({c['customer']['id']}). Payment {c['payment']['id']} is "
            f"{c['payment']['status']}"
            + (f" with reason {c['payment']['failure_reason']}"
               if c["payment"]["failure_reason"] else "")
            + f", amount {c['payment']['amount_cents']} {c['payment']['currency']}."
        )
