"""The agent loop.

Deliberately small. Five responsibilities:

  1. ask the planner what to do next
  2. run the tool (read tools record evidence; action tools go through policy)
  3. record every step in a trace the UI can show
  4. stop when an action is proposed, when the planner is done, or at the step cap
  5. execute only an action whose policy verdict is `allow`

The loop itself contains no domain logic. That is the point: swapping the rule
planner for an LLM changes judgement, not control flow.
"""
from __future__ import annotations

from typing import Any

from .. import db
from ..config import settings
from .brain import Plan, make_planner
from .tools import ToolRegistry


class AgentRun:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self.reg = ToolRegistry(incident_id)
        self.planner = make_planner(self.reg)
        self.trace: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        db.append_ledger("incident_started", {"planner": type(self.planner).__name__},
                         self.incident_id)

        action_id: str | None = None
        proposed: list[dict[str, Any]] = []
        executed: dict[str, Any] | None = None

        for _ in range(settings.max_agent_steps):
            plan: Plan | None = self.planner.next_action(
                [self._trace_line(s) for s in self.trace]
            )
            if plan is None:
                break

            step = self._run_tool(plan)
            self.trace.append(step)

            if step.get("error") == "stop":
                break

            if step["tool"] == "propose_action" and step.get("action_id"):
                proposed.append(step)
                verdict = step.get("verdict")
                atype = (plan.args or {}).get("type")
                # A confirmation changes no money, so the plan may continue
                # toward the action it unblocks. Anything else ends the run.
                if atype == "request_confirmation" and verdict != "deny":
                    continue
                action_id = step["action_id"]
                break

        if action_id:
            action = db.query_one("SELECT * FROM actions WHERE id = ?", (action_id,))
            if action and action["policy_verdict"] == "allow":
                try:
                    executed = self.reg.execute_action(action_id)
                except Exception as exc:
                    # Defense in depth: an execution failure must surface as a
                    # failed result, never as an uncaught 500 from the endpoint.
                    db.append_ledger(
                        "action_error",
                        {"action_id": action_id, "error": type(exc).__name__},
                        self.incident_id,
                    )
                    executed = {
                        "action_id": action_id,
                        "status": "failed",
                        "error": "action execution failed",
                    }

        return {
            "incident_id": self.incident_id,
            "planner": "llm" if settings.llm_enabled else "rules",
            "trace": self.trace,
            "actions": [
                {"action_id": p["action_id"], "verdict": p["verdict"],
                 "reason": p["reason"], "status": p["status"]}
                for p in proposed
            ],
            "action_id": action_id,
            "executed": executed,
        }

    def _run_tool(self, plan: Plan) -> dict[str, Any]:
        base = {"tool": plan.tool, "thought": plan.thought, "args": plan.args}
        try:
            if plan.tool == "propose_action":
                result = self.reg.propose_action(
                    type=plan.args.get("type", ""),
                    params=plan.args.get("params") or {},
                    evidence_ids=plan.args.get("evidence_ids") or [],
                )
                return {
                    **base,
                    **result,
                    "summary": f"proposed {result.get('action_id')} "
                               f"({result.get('verdict')})",
                }
            if plan.tool == "execute_action":
                result = self.reg.execute_action(plan.args["action_id"])
                return {
                    **base,
                    **result,
                    "summary": f"executed {result.get('action_id')}: "
                               f"{result.get('status')}",
                }
            if plan.tool == "stop":
                return {**base, "error": "stop", "summary": plan.thought}

            fn = getattr(self.reg, plan.tool, None)
            if fn is None:
                return {
                    **base,
                    "error": f"unknown tool {plan.tool}",
                    "summary": f"unknown tool {plan.tool}",
                }
            result = fn(**plan.args)
            return {**base, "evidence_id": result.get("evidence_id"),
                    "summary": self._summarise(plan.tool, result),
                    "data": result}
        except Exception as exc:  # a tool failure must not kill the run
            db.append_ledger("tool_error",
                             {"tool": plan.tool, "error": str(exc)},
                             self.incident_id)
            return {**base, "error": str(exc), "summary": f"error: {exc}"}

    @staticmethod
    def _trace_line(step: dict[str, Any]) -> str:
        """Every trace step is renderable, whatever path produced it."""
        detail = step.get("summary") or step.get("error") or "ok"
        return f"{step.get('tool', '?')}: {detail}"

    @staticmethod
    def _summarise(tool: str, result: dict[str, Any]) -> str:
        if tool == "check_risk":
            return f"risk {result['score']:.2f}"
        if tool == "search_policy":
            return f"matched policies: {', '.join(result['policies'])}"
        if tool == "get_customer_history":
            return f"{len(result['payments'])} payments"
        for key in ("payment", "order", "customer"):
            if key in result:
                row = result[key]
                return f"{key} {row.get('id', '')} {row.get('status', '')}".strip()
        return "ok"


def run_incident(incident_id: str) -> dict[str, Any]:
    return AgentRun(incident_id).run()
