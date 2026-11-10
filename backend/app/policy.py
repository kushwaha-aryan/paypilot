"""The deterministic policy engine.

Why this is not the LLM: the agent investigates and proposes. This module
decides whether the proposal is allowed, must be reviewed by a human, or is
forbidden outright. It is plain Python with no model call, so its behaviour is
identical every run, unit-testable, and explainable to a judge in one sentence.

Verdicts
    allow  -> execute immediately
    review -> queue for human approval, action is NOT executed
    deny   -> reject; the agent must revise its plan

The engine is intentionally conservative: anything it cannot positively justify
falls to `review`, never to `allow`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ALLOW = "allow"
REVIEW = "review"
DENY = "deny"

# Tunable guard rails. Merchant-specific limits live in the DB.
MAX_RETRY_ATTEMPTS = 3
MIN_RISK_FOR_FLAG = 0.5     # below this you cannot call something fraud
HIGH_RISK = 0.85            # at/above this, retrying a payment is too dangerous

VALID_ACTIONS = {
    "retry_payment",
    "refund",
    "flag_for_review",
    "request_confirmation",
    "close_incident",
}


@dataclass
class PolicyResult:
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    @property
    def executes(self) -> bool:
        return self.verdict == ALLOW


def _r(verdict: str, reason: str) -> PolicyResult:
    return PolicyResult(verdict=verdict, reasons=[reason])


def evaluate(action: dict[str, Any], ctx: dict[str, Any]) -> PolicyResult:
    """Judge one proposed action against the merchant's rules.

    `ctx` carries the facts the decision needs:
        payment      dict  the payment row
        order        dict  the order row
        customer     dict  the customer row
        merchant     dict  the merchant row (auto-refund limit lives here)
        incident     dict  the incident row
        risk_score   float computed risk for this incident
    """
    atype = action.get("type")
    params = action.get("params") or {}
    evidence_ids = action.get("evidence_ids") or []

    # ---- structural rules: these apply to every action -------------------
    if atype not in VALID_ACTIONS:
        return _r(DENY, f"unknown action type {atype!r}")

    if not evidence_ids:
        return _r(
            DENY,
            "action cites no evidence; every decision must reference the "
            "facts that justify it",
        )

    payment = ctx.get("payment") or {}
    merchant = ctx.get("merchant") or {}
    risk = float(ctx.get("risk_score") or 0.0)

    # ---- per-action rules ------------------------------------------------
    if atype == "retry_payment":
        return _evaluate_retry(payment, risk)

    if atype == "refund":
        return _evaluate_refund(payment, merchant, params)

    if atype == "flag_for_review":
        return _evaluate_flag(risk)

    if atype == "request_confirmation":
        # Always safe: it sends the customer a message and changes no money.
        return _r(ALLOW, "customer confirmation is non-destructive")

    if atype == "close_incident":
        return _evaluate_close(ctx)

    return _r(DENY, "unreachable")


def _evaluate_retry(payment: dict[str, Any], risk: float) -> PolicyResult:
    if payment.get("status") == "succeeded":
        return _r(DENY, "payment already succeeded; a retry would double-charge")

    if payment.get("status") == "refunded":
        return _r(DENY, "payment was refunded; retrying would recharge a "
                       "customer who already got their money back")

    attempts = int(payment.get("attempt_count") or 0)
    if attempts >= MAX_RETRY_ATTEMPTS:
        return _r(
            DENY,
            f"{attempts} failed attempts already; further retries risk "
            f"repeated authorisation holds on the customer's card",
        )

    if risk >= HIGH_RISK:
        return _r(DENY, f"risk score {risk:.2f} >= {HIGH_RISK}; do not move "
                        "money on a payment this suspicious")

    return _r(ALLOW, f"retry is permitted ({attempts}/{MAX_RETRY_ATTEMPTS} "
                     f"attempts used, risk {risk:.2f})")


def _evaluate_refund(
    payment: dict[str, Any], merchant: dict[str, Any], params: dict[str, Any]
) -> PolicyResult:
    if payment.get("status") != "succeeded":
        return _r(DENY, f"cannot refund a payment with status "
                        f"{payment.get('status')!r}")

    original = int(payment.get("amount_cents") or 0)
    requested = int(params.get("amount_cents") or 0)

    if requested <= 0:
        return _r(DENY, "refund amount must be positive")

    if requested > original:
        return _r(
            DENY,
            f"refund of {requested} exceeds the original payment of {original}",
        )

    limit = int(merchant.get("refund_auto_limit_cents") or 0)
    if requested <= limit:
        return _r(ALLOW, f"refund of {requested} is within the merchant's "
                         f"auto-refund limit of {limit}")

    return _r(
        REVIEW,
        f"refund of {requested} exceeds the merchant's auto-refund limit of "
        f"{limit}; requires human approval",
    )


def _evaluate_flag(risk: float) -> PolicyResult:
    if risk < MIN_RISK_FOR_FLAG:
        return _r(
            DENY,
            f"risk score {risk:.2f} is below {MIN_RISK_FOR_FLAG}; flagging as "
            f"fraud would be unsupported by the evidence",
        )
    if risk >= HIGH_RISK:
        return _r(ALLOW, f"risk score {risk:.2f} clearly justifies escalation")
    return _r(REVIEW, f"risk score {risk:.2f} is borderline; a human should "
                      f"confirm before the account is restricted")


def _evaluate_close(ctx: dict[str, Any]) -> PolicyResult:
    incident = ctx.get("incident") or {}
    if incident.get("kind") == "suspicious_charge":
        return _r(DENY, "suspicious-charge incidents need a human decision, "
                        "not an automatic close")
    return _r(ALLOW, "incident has a resolution path and no money is moved")
