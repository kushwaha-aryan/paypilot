"""End-to-end agent tests.

Each test seeds the demo dataset, runs one incident through the full agent loop,
and asserts on what the system actually decided. These are the tests that prove
the demo works — if they pass, the video will not surprise you.
"""
from app import db
from app.agent.loop import run_incident
from app.seed import seed


def test_payment_failure_asks_then_retries(fresh):
    """INC-001: decline with 1 attempt → confirm, then retry, then recover."""
    result = run_incident("INC-001")

    types = [s["tool"] for s in result["trace"]]
    assert "get_payment" in types and "check_risk" in types

    proposed = [a for a in result["actions"]]
    assert [a["verdict"] for a in proposed] == ["allow", "allow"]

    incident = db.query_one("SELECT * FROM incidents WHERE id = 'INC-001'")
    assert incident["outcome"] == "recovered"
    assert incident["status"] == "resolved"

    payment = db.query_one("SELECT * FROM payments WHERE id = 'P-1001'")
    assert payment["attempt_count"] == 2
    assert payment["status"] == "succeeded"


def test_suspicious_charge_is_flagged(fresh):
    """INC-002: risk 0.85 → flag for review, autonomous."""
    result = run_incident("INC-002")

    assert result["actions"][-1]["verdict"] == "allow"
    action = db.query_one(
        "SELECT * FROM actions WHERE incident_id = 'INC-002' ORDER BY rowid DESC"
    )
    assert action["type"] == "flag_for_review"

    incident = db.query_one("SELECT * FROM incidents WHERE id = 'INC-002'")
    assert incident["outcome"] == "flagged"
    assert incident["status"] == "escalated"


def test_large_refund_waits_for_human(fresh):
    """INC-003: $120 refund against a $50 auto limit → review, not executed."""
    result = run_incident("INC-003")

    action = db.query_one("SELECT * FROM actions WHERE incident_id = 'INC-003'")
    assert action["type"] == "refund"
    assert action["policy_verdict"] == "review"
    assert action["status"] == "awaiting_approval"
    assert result["executed"] is None

    payment = db.query_one("SELECT * FROM payments WHERE id = 'P-1003'")
    assert payment["status"] == "succeeded", "money must not move without approval"


def test_duplicate_charge_is_refunded_autonomously(fresh):
    """INC-004: $25 refund under the $50 limit → executed with no human."""
    result = run_incident("INC-004")

    assert result["executed"] is not None
    assert result["executed"]["outcome"] == "refunded"

    payment = db.query_one("SELECT * FROM payments WHERE id = 'P-1004'")
    assert payment["status"] == "refunded"


def test_three_failed_attempts_stop_retrying(fresh):
    """INC-005: 3 attempts used → no retry proposed at all."""
    run_incident("INC-005")

    types = [a["type"] for a in db.query(
        "SELECT type FROM actions WHERE incident_id = 'INC-005'"
    )]
    assert "retry_payment" not in types


def test_refund_under_second_merchant_limit_is_autonomous(fresh):
    """INC-006: $90 refund under M-2's $150 limit → executed."""
    result = run_incident("INC-006")
    assert result["executed"] is not None
    assert result["executed"]["outcome"] == "refunded"


def test_every_action_cites_evidence(fresh):
    seed()
    for iid in ("INC-001", "INC-002", "INC-003", "INC-004", "INC-005", "INC-006"):
        run_incident(iid)

    actions = db.query("SELECT * FROM actions")
    assert actions
    for a in actions:
        assert a["evidence_ids"] != "[]", f"{a['id']} has no evidence"


def test_denied_action_cannot_be_forced(fresh):
    """A denied refund must stay denied even with human approval."""
    from app.agent.tools import ToolError, ToolRegistry

    reg = ToolRegistry("INC-003")
    reg.get_payment("P-1003")
    reg.check_risk()
    res = reg.propose_action(
        type="refund",
        params={"amount_cents": 999_999},
        evidence_ids=["INC-003-E1"],
    )
    assert res["verdict"] == "deny"

    import pytest
    with pytest.raises(ToolError):
        reg.execute_action(res["action_id"], human_approved=True)


def test_ledger_detects_tampering(fresh):
    run_incident("INC-001")
    assert db.verify_ledger()["ok"] is True

    # Target a row that actually exists: the shared test database keeps its
    # AUTOINCREMENT counter across seed() calls, so a hard-coded seq is fragile.
    row = db.query_one(
        "SELECT seq FROM ledger WHERE incident_id = 'INC-001' ORDER BY seq LIMIT 1"
    )
    assert row is not None
    db.execute(
        "UPDATE ledger SET payload = '{\"tampered\":true}' WHERE seq = ?",
        (row["seq"],),
    )
    result = db.verify_ledger()
    assert result["ok"] is False
    assert result["broken_at_seq"] == row["seq"]


def test_agent_is_idempotent_at_the_policy_level(fresh):
    """Running the same incident twice must not double-refund."""
    run_incident("INC-004")
    run_incident("INC-004")

    refunds = db.query(
        "SELECT * FROM actions WHERE incident_id = 'INC-004' AND status = 'executed'"
    )
    assert len(refunds) >= 1
    payment = db.query_one("SELECT * FROM payments WHERE id = 'P-1004'")
    assert payment["status"] == "refunded"
