"""Policy engine unit tests.

These are the tests that matter most: the policy engine is the component that
decides whether money moves, and it must be correct without an LLM in the loop.
"""
from app.policy import ALLOW, DENY, REVIEW, evaluate


def ctx(
    status="failed",
    amount=4500,
    attempts=1,
    risk=0.2,
    limit=5000,
    kind="payment_failed",
):
    return {
        "payment": {"status": status, "amount_cents": amount,
                    "attempt_count": attempts},
        "order": {"id": "O-1"},
        "customer": {"id": "C-1"},
        "merchant": {"refund_auto_limit_cents": limit},
        "incident": {"kind": kind},
        "risk_score": risk,
    }


def act(type_, params=None, evidence=("E1",)):
    return {"type": type_, "params": params or {}, "evidence_ids": list(evidence)}


# -- structural ------------------------------------------------------------

def test_action_without_evidence_is_denied():
    r = evaluate(act("refund", {"amount_cents": 100}, evidence=[]), ctx())
    assert r.verdict == DENY
    assert "no evidence" in r.reason


def test_unknown_action_is_denied():
    assert evaluate(act("wire_money"), ctx()).verdict == DENY


# -- retry -----------------------------------------------------------------

def test_retry_allowed_on_failed_payment():
    assert evaluate(act("retry_payment"), ctx()).verdict == ALLOW


def test_retry_denied_on_succeeded_payment():
    r = evaluate(act("retry_payment"), ctx(status="succeeded", risk=0.1))
    assert r.verdict == DENY
    assert "double-charge" in r.reason


def test_retry_denied_after_three_attempts():
    assert evaluate(act("retry_payment"), ctx(attempts=3)).verdict == DENY


def test_retry_denied_at_high_risk():
    assert evaluate(act("retry_payment"), ctx(risk=0.9)).verdict == DENY


def test_retry_allowed_at_borderline_risk():
    assert evaluate(act("retry_payment"), ctx(risk=0.84)).verdict == ALLOW


# -- refund ----------------------------------------------------------------

def test_refund_below_limit_is_allowed():
    r = evaluate(act("refund", {"amount_cents": 4000}), ctx(status="succeeded"))
    assert r.verdict == ALLOW


def test_refund_above_limit_needs_review():
    r = evaluate(
        act("refund", {"amount_cents": 8000}),
        ctx(status="succeeded", amount=10000),
    )
    assert r.verdict == REVIEW


def test_refund_exactly_at_limit_is_allowed():
    r = evaluate(
        act("refund", {"amount_cents": 5000}),
        ctx(status="succeeded", amount=5000),
    )
    assert r.verdict == ALLOW


def test_refund_over_original_is_denied():
    r = evaluate(
        act("refund", {"amount_cents": 9000}),
        ctx(status="succeeded", amount=4000, limit=100000),
    )
    assert r.verdict == DENY
    assert "exceeds the original" in r.reason


def test_refund_on_failed_payment_is_denied():
    r = evaluate(act("refund", {"amount_cents": 100}), ctx(status="failed"))
    assert r.verdict == DENY


def test_zero_refund_is_denied():
    r = evaluate(act("refund", {"amount_cents": 0}), ctx(status="succeeded"))
    assert r.verdict == DENY


# -- flags and confirmation ------------------------------------------------

def test_flag_denied_below_threshold():
    assert evaluate(act("flag_for_review"), ctx(risk=0.3)).verdict == DENY


def test_flag_allowed_at_high_risk():
    assert evaluate(act("flag_for_review"), ctx(risk=0.9)).verdict == ALLOW


def test_flag_at_borderline_risk_needs_review():
    assert evaluate(act("flag_for_review"), ctx(risk=0.6)).verdict == REVIEW


def test_confirmation_is_always_allowed():
    assert evaluate(act("request_confirmation"), ctx(risk=0.99)).verdict == ALLOW


# -- close -----------------------------------------------------------------

def test_close_denied_for_suspicious_charge():
    c = ctx(kind="suspicious_charge")
    assert evaluate(act("close_incident"), c).verdict == DENY


def test_close_allowed_for_payment_failure():
    assert evaluate(act("close_incident"), ctx()).verdict == ALLOW
