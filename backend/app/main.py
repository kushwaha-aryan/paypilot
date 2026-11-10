"""FastAPI app: the HTTP surface over the agent.

The API is intentionally thin. It reads the database, calls the agent loop, and
records human decisions. No business logic lives here — that keeps the policy
engine the single place where "what is allowed" is decided.
"""
from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, incidents, sandbox
from .agent.loop import run_incident
from .agent.tools import ToolRegistry
from .config import DEFAULT_CHECKOUT_CURRENCY, settings
from .payment_client import PayPilotClient

app = FastAPI(
    title="PayPilot",
    description="Autonomous payment recovery & risk agent with a policy gate.",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)

STATIC = Path(__file__).parent / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _page(filename: str, missing: str) -> FileResponse:
    page = STATIC / filename
    if not page.exists():
        raise HTTPException(404, missing)
    return FileResponse(page)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/")
def index() -> FileResponse:
    return _page("dashboard.html", "dashboard not built")


@app.get("/architecture", include_in_schema=False)
def architecture() -> FileResponse:
    return _page("architecture.html", "architecture page not built")


@app.get("/docs", include_in_schema=False)
def api_docs() -> FileResponse:
    return _page("docs.html", "docs page not built")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "llm": "anthropic" if settings.llm_enabled else "deterministic-rules",
        "paypal": "live-sandbox" if settings.paypal_live else "mock",
    }


# ---------------------------------------------------------------------------
# Sandbox checkout (create -> buyer approval -> capture)
# ---------------------------------------------------------------------------

class SandboxOrderRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000)
    currency: str = Field(
        default=DEFAULT_CHECKOUT_CURRENCY, min_length=3, max_length=3
    )
    merchant_id: str = "M-1"
    customer_id: str = "C-1"


@app.post("/api/sandbox/orders", status_code=201)
def sandbox_create_order(body: SandboxOrderRequest) -> dict[str, Any]:
    """Create a sandbox order and return the PayPal approval URL."""
    try:
        return sandbox.create_test_order(
            body.amount_cents,
            body.currency,
            body.merchant_id,
            body.customer_id,
        )
    except sandbox.SandboxError as exc:
        raise HTTPException(exc.status_code, exc.message)


@app.post("/api/sandbox/orders/{order_id}/capture")
def sandbox_capture_order(order_id: str) -> dict[str, Any]:
    """Capture an approved sandbox order and persist PayPal's capture id."""
    try:
        return sandbox.capture_test_order(order_id)
    except sandbox.SandboxError as exc:
        raise HTTPException(exc.status_code, exc.message)


# ---------------------------------------------------------------------------
# Live PayPal proof (read-only)
# ---------------------------------------------------------------------------

_REFUND_ID_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")


@lru_cache(maxsize=1)
def _paypal_client() -> PayPilotClient:
    """One client per process so the OAuth token is reused between requests."""
    return PayPilotClient()


@app.get("/api/paypal/refunds/{refund_id}")
def paypal_refund(refund_id: str) -> dict[str, Any]:
    """Read an existing PayPal Sandbox refund. Never writes, never moves money.

    The id must already be recorded on a refund action, so this endpoint cannot
    be used to probe arbitrary sandbox refund ids. A single GET is forwarded and
    the response is trimmed to the fields the dashboard shows.
    """
    if not _REFUND_ID_RE.fullmatch(refund_id):
        raise HTTPException(404, "refund not found")
    known = db.query_one(
        "SELECT id FROM actions WHERE type = 'refund' "
        "AND json_extract(result, '$.id') = ?",
        (refund_id,),
    )
    if not known:
        raise HTTPException(404, "refund not found")

    started = time.perf_counter()
    result = _paypal_client().get_refund(refund_id)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    if not result.get("ok"):
        error = result.get("error") or {}
        raise HTTPException(
            502,
            {
                "message": error.get("message") or "PayPal refund lookup failed",
                "name": error.get("name"),
                "debug_id": error.get("debug_id"),
            },
        )

    amount = result.get("amount") or {}
    return {
        "id": result.get("id") or refund_id,
        "status": result.get("status"),
        "amount": {
            "value": amount.get("value"),
            "currency_code": amount.get("currency_code"),
        },
        "create_time": result.get("create_time"),
        "note_to_payer": result.get("note_to_payer"),
        "elapsed_ms": elapsed_ms,
        "source": "live-sandbox" if result.get("mode") == "live" else "mock",
    }


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

class RefundIncidentRequest(BaseModel):
    payment_id: str = Field(min_length=1)


@app.post("/api/incidents/refund-request", status_code=201)
def create_refund_request(
    body: RefundIncidentRequest, response: Response
) -> dict[str, Any]:
    """Create an open refund_request incident for a captured sandbox payment.

    Idempotent per payment: an existing open/awaiting_approval incident is
    returned with HTTP 200 and ``created: false``. Creating an incident never
    calls PayPal and never moves money.
    """
    try:
        result = incidents.create_refund_incident(body.payment_id)
    except incidents.IncidentError as exc:
        raise HTTPException(exc.status_code, exc.message)
    if not result["created"]:
        response.status_code = 200
    return result


@app.get("/api/incidents")
def list_incidents() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT i.*, o.amount_cents, o.currency, c.name AS customer_name, "
        "m.name AS merchant_name, p.status AS payment_status, "
        "p.failure_reason, p.attempt_count, p.risk_score "
        "FROM incidents i "
        "JOIN orders o ON o.id = i.order_id "
        "JOIN customers c ON c.id = o.customer_id "
        "JOIN merchants m ON m.id = i.merchant_id "
        "JOIN payments p ON p.id = i.payment_id "
        "ORDER BY i.created_at DESC"
    )
    for r in rows:
        actions = db.query(
            "SELECT status, policy_verdict FROM actions WHERE incident_id = ?",
            (r["id"],),
        )
        r["action_count"] = len(actions)
        r["needs_approval"] = any(a["status"] == "awaiting_approval" for a in actions)
    return rows


@app.get("/api/incidents/{incident_id}")
def incident_detail(incident_id: str) -> dict[str, Any]:
    incident = db.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not incident:
        raise HTTPException(404, "incident not found")

    reg = ToolRegistry(incident_id)
    ctx = reg.context()

    evidence = db.query(
        "SELECT * FROM evidence WHERE incident_id = ? ORDER BY created_at",
        (incident_id,),
    )
    for e in evidence:
        e["payload"] = json.loads(e["payload"])

    actions = db.query(
        "SELECT * FROM actions WHERE incident_id = ? ORDER BY rowid DESC",
        (incident_id,),
    )
    for a in actions:
        a["params"] = json.loads(a["params"])
        a["evidence_ids"] = json.loads(a["evidence_ids"])
        a["result"] = json.loads(a["result"]) if a["result"] else None

    ledger = db.query(
        "SELECT seq, ts, kind, payload FROM ledger WHERE incident_id = ? "
        "ORDER BY seq",
        (incident_id,),
    )
    for entry in ledger:
        entry["payload"] = json.loads(entry["payload"])

    return {
        "incident": incident,
        "payment": ctx["payment"],
        "order": ctx["order"],
        "customer": {**ctx["customer"],
                     "known_device_ids": json.loads(ctx["customer"]["known_device_ids"])},
        "merchant": ctx["merchant"],
        "evidence": evidence,
        "actions": actions,
        "ledger": ledger,
    }


@app.post("/api/incidents/{incident_id}/run")
def run(incident_id: str) -> dict[str, Any]:
    if not db.query_one("SELECT id FROM incidents WHERE id = ?", (incident_id,)):
        raise HTTPException(404, "incident not found")
    return run_incident(incident_id)


# ---------------------------------------------------------------------------
# Human decisions
# ---------------------------------------------------------------------------

@app.post("/api/actions/{action_id}/approve")
def approve(action_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM actions WHERE id = ?", (action_id,))
    if not row:
        raise HTTPException(404, "action not found")
    if row["policy_verdict"] == "deny":
        raise HTTPException(409, "policy denied this action; it cannot be approved")
    if row["status"] == "executed":
        raise HTTPException(409, "action already executed")

    db.append_ledger(
        "human_approved",
        {"action_id": action_id, "policy_verdict": row["policy_verdict"]},
        row["incident_id"],
    )
    try:
        return ToolRegistry(row["incident_id"]).execute_action(
            action_id, human_approved=True
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/actions/{action_id}/reject")
def reject(action_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM actions WHERE id = ?", (action_id,))
    if not row:
        raise HTTPException(404, "action not found")
    db.execute("UPDATE actions SET status = 'rejected' WHERE id = ?", (action_id,))
    db.execute(
        "UPDATE incidents SET status = 'open' WHERE id = ?", (row["incident_id"],)
    )
    db.append_ledger(
        "human_rejected", {"action_id": action_id}, row["incident_id"]
    )
    return {"action_id": action_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# Audit + metrics
# ---------------------------------------------------------------------------

@app.get("/api/ledger/verify")
def ledger_verify() -> dict[str, Any]:
    return db.verify_ledger()


@app.get("/api/ledger")
def ledger_tail(limit: int = 100) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT seq, ts, kind, incident_id, payload, hash FROM ledger "
        "ORDER BY seq DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        r["payload"] = json.loads(r["payload"])
        r["hash"] = r["hash"][:16] + "…"
    return rows


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    incidents = db.query("SELECT status, outcome FROM incidents")
    actions = db.query("SELECT status, policy_verdict, type FROM actions")
    recovered = db.query(
        "SELECT COALESCE(SUM(o.amount_cents), 0) AS cents, COUNT(*) AS n "
        "FROM incidents i JOIN orders o ON o.id = i.order_id "
        "WHERE i.outcome = 'recovered'"
    )[0]

    executed = [a for a in actions if a["status"] == "executed"]
    autonomous = [a for a in executed if a["policy_verdict"] == "allow"]

    return {
        "incidents": {
            "total": len(incidents),
            "open": sum(1 for i in incidents if i["status"] == "open"),
            "awaiting_approval": sum(
                1 for i in incidents if i["status"] == "awaiting_approval"
            ),
            "resolved": sum(1 for i in incidents if i["status"] == "resolved"),
            "escalated": sum(1 for i in incidents if i["status"] == "escalated"),
        },
        "recovered": {
            "count": recovered["n"],
            "amount_cents": recovered["cents"],
        },
        "actions": {
            "proposed": len(actions),
            "executed": len(executed),
            "autonomous": len(autonomous),
            "human_approved": len(executed) - len(autonomous),
            "blocked": sum(1 for a in actions if a["status"] == "rejected"),
        },
        "autonomy_rate": round(len(autonomous) / len(executed), 3) if executed else 0.0,
    }
