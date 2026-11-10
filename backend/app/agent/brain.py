"""Two planners behind one interface.

RulePlanner   — deterministic, no network, always works. This is the path that
                runs by default and the path the test suite exercises.
LLMPlanner    — takes the structured tool list and the incident briefing, asks
                the model which tools to call, and lets the tool registry and
                policy engine enforce the limits regardless of what it says.

Both produce the same output shape, so the loop that consumes them is identical.
The important property: the LLM is not special. It gets no capability the
rules planner does not have, and no ability to bypass the policy engine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from .tools import ToolRegistry


@dataclass
class Plan:
    thought: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


class RulePlanner:
    """Deterministic decision tree used as the default offline planner.

    It implements the same investigation and proposal flow expected of the
    optional LLM planner, providing a stable reference behavior.
    """

    def __init__(self, registry: ToolRegistry):
        self.reg = registry
        self.step = 0
        self.proposed: list[str] = []
        self._case: dict[str, Any] | None = None

    def _context(self) -> dict[str, Any]:
        """The incident case file, loaded once and reused for the whole run."""
        if self._case is None:
            self._case = self.reg.context()
        return self._case

    def next_action(self, trace: list[str]) -> Plan | None:
        self.step += 1
        case = self._context()
        payment_id = case["payment"]["id"]
        order_id = case["order"]["id"]
        customer_id = case["customer"]["id"]

        if self.step == 1:
            return Plan("Collect the facts for this incident.", "get_payment",
                        {"payment_id": payment_id})
        if self.step == 2:
            return Plan("Look at the order for context.", "get_order",
                        {"order_id": order_id})
        if self.step == 3:
            return Plan("Check the customer's history and account age.",
                        "get_customer", {"customer_id": customer_id})
        if self.step == 4:
            return Plan("Check prior payments from this customer.",
                        "get_customer_history", {"customer_id": customer_id})
        if self.step == 5:
            return Plan("Score the risk deterministically.", "check_risk")
        if self.step == 6:
            return Plan("Look up the policy that governs this incident kind.",
                        "search_policy",
                        {"query": "refunds retries fraud disputes"})
        if self.step >= 7:
            plan = self._decide()
            if plan.tool == "propose_action":
                self.proposed.append(plan.args.get("type", ""))
            return plan
        return None

    def _decide(self) -> Plan:
        ctx = self._context()
        incident = ctx["incident"]
        payment = ctx["payment"]
        risk = self._risk()
        evidence = self._evidence_ids()

        kind = incident["kind"]

        if kind == "suspicious_charge":
            if risk >= 0.5:
                return Plan(
                    f"Risk is {risk:.2f}, above the 0.5 fraud threshold. "
                    f"Flag the account for human review.",
                    "propose_action",
                    {
                        "type": "flag_for_review",
                        "params": {"reason": f"risk score {risk:.2f}"},
                        "evidence_ids": evidence,
                    },
                )
            return Plan(
                f"Risk is only {risk:.2f}. Close as a false positive.",
                "propose_action",
                {"type": "close_incident", "params": {}, "evidence_ids": evidence},
            )

        if kind == "refund_request":
            return Plan(
                "Refund the full original amount; the policy engine will "
                "decide whether that needs a human.",
                "propose_action",
                {
                    "type": "refund",
                    "params": {
                        "amount_cents": int(payment["amount_cents"]),
                        "reason": "customer refund request",
                    },
                    "evidence_ids": evidence,
                },
            )

        if kind == "duplicate_charge":
            return Plan(
                "Duplicate confirmed from the payment records; refund the "
                "duplicate amount.",
                "propose_action",
                {
                    "type": "refund",
                    "params": {
                        "amount_cents": int(payment["amount_cents"]),
                        "reason": "confirmed duplicate charge",
                    },
                    "evidence_ids": evidence,
                },
            )

        # payment_failed
        attempts = int(payment["attempt_count"] or 0)
        reason = (payment["failure_reason"] or "").lower()

        if risk >= 0.85:
            return Plan(
                f"Risk is {risk:.2f}, too high to touch the payment. Escalate.",
                "propose_action",
                {
                    "type": "flag_for_review",
                    "params": {"reason": f"high risk {risk:.2f} on failed payment"},
                    "evidence_ids": evidence,
                },
            )

        if "insufficient" in reason or "declin" in reason:
            if attempts >= 3:
                if "request_confirmation" in self.proposed:
                    return Plan(
                        "Customer has been notified and the retry budget is "
                        "exhausted. No further automatic action is safe.",
                        "stop",
                    )
                return Plan(
                    "Three attempts used; stop retrying and inform the customer.",
                    "propose_action",
                    {
                        "type": "request_confirmation",
                        "params": {"channel": "email",
                                   "message": "payment keeps failing; please update method"},
                        "evidence_ids": evidence,
                    },
                )
            if "request_confirmation" not in self.proposed:
                return Plan(
                    f"Failure is a decline ({reason}) with {attempts} attempt(s) "
                    f"used. Ask the customer to confirm, then retry.",
                    "propose_action",
                    {
                        "type": "request_confirmation",
                        "params": {"channel": "email"},
                        "evidence_ids": evidence,
                    },
                )
            return Plan(
                "Customer was asked to confirm; the retry is now safe to "
                "propose and is bounded by the retry policy.",
                "propose_action",
                {
                    "type": "retry_payment",
                    "params": {"amount_cents": int(payment["amount_cents"])},
                    "evidence_ids": evidence,
                },
            )

        if "request_confirmation" in self.proposed:
            return Plan(
                "Customer was asked for confirmation; awaiting their reply.",
                "stop",
            )
        return Plan(
            "Unclassified failure; ask the customer for confirmation.",
            "propose_action",
            {"type": "request_confirmation", "params": {}, "evidence_ids": evidence},
        )

    def _risk(self) -> float:
        import json as _json
        from .. import db

        row = db.query_one(
            "SELECT payload FROM evidence WHERE incident_id = ? AND tool = 'check_risk' "
            "ORDER BY created_at DESC LIMIT 1",
            (self.reg.incident_id,),
        )
        return _json.loads(row["payload"])["score"] if row else 0.0

    def _evidence_ids(self) -> list[str]:
        from .. import db

        rows = db.query(
            "SELECT id FROM evidence WHERE incident_id = ? ORDER BY created_at",
            (self.reg.incident_id,),
        )
        return [r["id"] for r in rows]


class LLMPlanner:
    """Model-driven planner. Same capabilities, different judgement."""

    def __init__(self, registry: ToolRegistry):
        self.reg = registry
        self.step = 0

    def next_action(self, trace: list[str]) -> Plan | None:
        from anthropic import Anthropic

        self.step += 1
        client = Anthropic(api_key=settings.anthropic_api_key)

        system = (
            "You are PayPilot, an autonomous payment-operations agent for an "
            "online merchant. Investigate the incident using tools, then propose "
            "exactly one action. Rules you must follow:\n"
            "- Cite evidence ids in evidence_ids for every proposed action.\n"
            "- Never retry a payment that already succeeded.\n"
            "- Never refund more than the original payment amount.\n"
            "- A fraud flag requires risk score >= 0.5.\n"
            "- When you have proposed an action, stop.\n"
            "Respond only with JSON: "
            '{"thought": str, "tool": str, "args": {...}} or {"done": true,'
            ' "summary": str}.'
        )

        tools = [
            {"name": "get_payment", "args": {}},
            {"name": "get_order", "args": {}},
            {"name": "get_customer", "args": {}},
            {"name": "get_customer_history", "args": {}},
            {"name": "check_risk", "args": {}},
            {"name": "search_policy", "args": {"query": "str"}},
            {"name": "propose_action",
             "args": {"type": "str", "params": "object",
                      "evidence_ids": "array[str]"}},
            {"name": "execute_action", "args": {"action_id": "str"}},
        ]

        user = json.dumps({
            "incident": self.reg.describe_incident(),
            "available_tools": tools,
            "steps_so_far": trace,
        })

        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        try:
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            return Plan("Model returned unparseable output; stopping.", "stop")

        if data.get("done"):
            return None
        return Plan(
            thought=data.get("thought", ""),
            tool=data.get("tool", "stop"),
            args=data.get("args") or {},
        )


def make_planner(registry: ToolRegistry):
    return LLMPlanner(registry) if settings.llm_enabled else RulePlanner(registry)
