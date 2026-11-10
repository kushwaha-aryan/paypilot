"""Mocked tests for the Gemini planner integration.

These tests never call the real Gemini API and never touch payment/refund flows.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import replace

from app import config
from app.agent.brain import GeminiPlanner, Plan, RulePlanner, make_planner
from app.agent.tools import ToolRegistry


def _with_settings(monkeypatch, **overrides):
    monkeypatch.setattr(config, "settings", replace(config.settings, **overrides))


class FakeRegistry:
    incident_id = "INC-TEST"

    def describe_incident(self) -> str:
        return "Incident INC-TEST (payment_failed) for order O-1."


def _install_fake_genai(monkeypatch, response_text: str):
    calls: dict[str, object] = {}

    class FakeModels:
        def generate_content(self, *, model, contents):
            calls["model"] = model
            calls["contents"] = contents
            return types.SimpleNamespace(text=response_text)

    class FakeClient:
        def __init__(self, api_key=None):
            calls["api_key"] = api_key
            self.models = FakeModels()

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    return calls


def test_make_planner_prefers_gemini_when_configured(monkeypatch):
    _with_settings(monkeypatch, gemini_api_key="test-key", anthropic_api_key="other-key")
    planner = make_planner(ToolRegistry("INC-001"))
    assert isinstance(planner, GeminiPlanner)


def test_make_planner_falls_back_to_rules_without_keys(monkeypatch):
    _with_settings(monkeypatch, gemini_api_key=None, anthropic_api_key=None)
    planner = make_planner(ToolRegistry("INC-001"))
    assert isinstance(planner, RulePlanner)


def test_gemini_planner_parses_structured_tool_plan(monkeypatch):
    payload = {
        "thought": "Collect payment facts first.",
        "tool": "get_payment",
        "args": {"payment_id": "P-1001"},
    }
    calls = _install_fake_genai(monkeypatch, json.dumps(payload))
    _with_settings(monkeypatch, gemini_api_key="test-key", gemini_model="gemini-test-model")

    planner = GeminiPlanner(FakeRegistry())
    plan = planner.next_action([])

    assert isinstance(plan, Plan)
    assert plan.tool == "get_payment"
    assert plan.args == {"payment_id": "P-1001"}
    assert plan.thought == "Collect payment facts first."
    assert calls["model"] == "gemini-test-model"
    assert "Incident INC-TEST" in str(calls["contents"])


def test_gemini_planner_parses_done_response(monkeypatch):
    _install_fake_genai(monkeypatch, '{"done": true, "summary": "complete"}')
    _with_settings(monkeypatch, gemini_api_key="test-key")

    planner = GeminiPlanner(FakeRegistry())
    assert planner.next_action([]) is None


def test_gemini_planner_validates_propose_action_payload(monkeypatch):
    payload = {
        "thought": "Propose a review flag.",
        "tool": "propose_action",
        "args": {
            "type": "flag_for_review",
            "params": {"reason": "risk"},
            "evidence_ids": ["INC-TEST-E1", "INC-TEST-E2"],
        },
    }
    _install_fake_genai(monkeypatch, json.dumps(payload))
    _with_settings(monkeypatch, gemini_api_key="test-key")

    plan = GeminiPlanner(FakeRegistry()).next_action([])

    assert plan is not None
    assert plan.tool == "propose_action"
    assert plan.args["type"] == "flag_for_review"
    assert plan.args["evidence_ids"] == ["INC-TEST-E1", "INC-TEST-E2"]


def test_gemini_planner_rejects_invalid_output(monkeypatch):
    _install_fake_genai(monkeypatch, "not json")
    _with_settings(monkeypatch, gemini_api_key="test-key")

    plan = GeminiPlanner(FakeRegistry()).next_action([])

    assert plan is not None
    assert plan.tool == "stop"


def test_gemini_planner_rejects_unsupported_tool(monkeypatch):
    payload = {"thought": "bad", "tool": "delete_database", "args": {}}
    _install_fake_genai(monkeypatch, json.dumps(payload))
    _with_settings(monkeypatch, gemini_api_key="test-key")

    plan = GeminiPlanner(FakeRegistry()).next_action([])

    assert plan is not None
    assert plan.tool == "stop"


def test_gemini_planner_fails_safe_on_request_error(monkeypatch):
    class BoomModels:
        def generate_content(self, **kwargs):
            raise RuntimeError("boom")

    class BoomClient:
        def __init__(self, api_key=None):
            self.models = BoomModels()

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = BoomClient
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    _with_settings(monkeypatch, gemini_api_key="test-key")

    plan = GeminiPlanner(FakeRegistry()).next_action([])

    assert plan is not None
    assert plan.tool == "stop"
