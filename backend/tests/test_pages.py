"""Regression coverage for the static UI shell.

The dashboard, architecture, and docs pages are served by FastAPI and share one
navigation contract. These tests keep the routes wired and confirm the incident
list still exposes the additive risk field the dashboard renders.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(fresh):
    with TestClient(app) as test_client:
        yield test_client


def test_ui_pages_share_navigation(client):
    for path, marker in (
        ("/", "incident-list"),
        ("/architecture", "Policy gate"),
        ("/docs", "SwaggerUIBundle"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text
        for href in ('href="/"', 'href="/architecture"', 'href="/docs"'):
            assert href in response.text
        assert "/static/app.css" in response.text


def test_architecture_page_has_policy_branches(client):
    text = client.get("/architecture").text
    for label in (
        "Incident",
        "Gemini Planner",
        "Structured Plan",
        "Tool Registry",
        "Deterministic Policy",
        "Human Approval",
        "PayPal",
        "Result",
        "allow / review / deny",
        "AI-generated planning",
        "Deterministic policy enforcement",
    ):
        assert label in text


def test_openapi_keeps_api_contracts(client):
    schema = client.get("/openapi.json").json()
    assert "/api/incidents/refund-request" in schema["paths"]
    assert "/api/ledger/verify" in schema["paths"]
    assert "/docs" not in schema["paths"]
    assert "/architecture" not in schema["paths"]


def test_incident_list_exposes_risk_score(client):
    rows = client.get("/api/incidents").json()
    assert rows
    assert all("risk_score" in row for row in rows)
