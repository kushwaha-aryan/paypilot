"""Offline tests for the sandbox checkout flow.

No test here touches the network. The service is exercised with a fake PayPal
client, and the adapter's live request shape is checked with a fake httpx.post.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.payment_client as payment_client
from app import db, sandbox
from app.main import app
from app.payment_client import SANDBOX_API_BASE, PayPilotClient


class FakePayPalClient:
    def __init__(
        self,
        *,
        create_ok: bool = True,
        order_status: str = "APPROVED",
        capture_ok: bool = True,
        capture_status: str = "COMPLETED",
        capture_id: str = "CAP-123",
    ):
        self.create_ok = create_ok
        self.order_status = order_status
        self.capture_ok = capture_ok
        self.capture_status = capture_status
        self.capture_id = capture_id
        self.calls: list[tuple] = []

    def create_order(self, amount_cents, currency="USD", *, request_id=None,
                     reference_id=None):
        self.calls.append(
            ("create_order", amount_cents, currency, request_id, reference_id)
        )
        if not self.create_ok:
            return {
                "ok": False,
                "mode": "fake",
                "error": {
                    "http_status": 422,
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "order rejected",
                    "debug_id": "debug-1",
                    "details": [
                        {
                            "issue": "CURRENCY_NOT_SUPPORTED",
                            "description": "Currency code is not supported.",
                        },
                    ],
                },
            }
        return {
            "ok": True,
            "mode": "fake",
            "id": "PP-ORDER-1",
            "status": "CREATED",
            "links": [{
                "rel": "payer-action",
                "href": "https://www.sandbox.paypal.com/checkoutnow?token=PP-ORDER-1",
                "method": "GET",
            }],
        }

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {
            "ok": True,
            "mode": "fake",
            "id": order_id,
            "status": self.order_status,
        }

    def capture_order(self, order_id, *, request_id=None):
        self.calls.append(("capture_order", order_id, request_id))
        if not self.capture_ok:
            return {
                "ok": False,
                "mode": "fake",
                "error": {
                    "http_status": 422,
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "capture rejected",
                    "debug_id": "debug-2",
                },
            }
        capture_status = (
            "COMPLETED" if self.capture_status == "COMPLETED" else "FAILED"
        )
        return {
            "ok": True,
            "mode": "fake",
            "id": order_id,
            "status": self.capture_status,
            "purchase_units": [{
                "reference_id": "default",
                "payments": {
                    "captures": [{
                        "id": self.capture_id,
                        "status": capture_status,
                    }],
                },
            }],
        }


def _create(fake, amount_cents=1234):
    return sandbox.create_test_order(
        amount_cents, merchant_id="M-1", customer_id="C-1", client=fake
    )


def test_create_persists_external_ids_and_returns_approval_url(fresh):
    fake = FakePayPalClient()

    result = _create(fake)

    assert result["order_id"].startswith("O-SBX-")
    assert result["payment_id"].startswith("P-SBX-")
    assert result["approval_url"].startswith("https://www.sandbox.paypal.com/")

    order = db.query_one(
        "SELECT * FROM orders WHERE id = ?", (result["order_id"],)
    )
    payment = db.query_one(
        "SELECT * FROM payments WHERE id = ?", (result["payment_id"],)
    )
    assert order["paypal_order_id"] == "PP-ORDER-1"
    assert order["amount_cents"] == 1234
    assert order["currency"] == "USD"
    assert payment["paypal_order_id"] == "PP-ORDER-1"
    assert payment["currency"] == "USD"
    assert payment["status"] == "pending"
    assert payment["paypal_capture_id"] is None

    # Existing seeded internal ids are untouched.
    assert db.query_one("SELECT id FROM payments WHERE id = 'P-1001'")

    create_call = fake.calls[0]
    assert create_call[2] == "USD"
    assert create_call[3] == f"paypilot-create-{result['order_id']}"
    assert create_call[4] == result["order_id"]


def test_create_defaults_to_usd(fresh):
    fake = FakePayPalClient()

    result = sandbox.create_test_order(
        1234, merchant_id="M-1", customer_id="C-1", client=fake
    )

    assert result["currency"] == "USD"
    assert fake.calls[0][2] == "USD"


def test_create_forwards_explicit_inr_override(fresh):
    fake = FakePayPalClient()

    result = sandbox.create_test_order(
        1234, "inr", "M-1", "C-1", client=fake
    )

    assert result["currency"] == "INR"
    assert fake.calls[0][2] == "INR"
    order = db.query_one(
        "SELECT * FROM orders WHERE id = ?", (result["order_id"],)
    )
    assert order["currency"] == "INR"


def test_create_failure_leaves_payment_pending(fresh):
    fake = FakePayPalClient(create_ok=False)

    with pytest.raises(sandbox.SandboxError) as exc:
        _create(fake)

    assert exc.value.status_code == 502
    assert "CURRENCY_NOT_SUPPORTED" in exc.value.message
    assert "Currency code is not supported." in exc.value.message
    payment = db.query_one(
        "SELECT * FROM payments WHERE failure_reason LIKE 'PayPal:%'"
    )
    assert payment["status"] == "pending"
    assert payment["paypal_order_id"] is None
    assert "CURRENCY_NOT_SUPPORTED" in payment["failure_reason"]
    assert "Currency code is not supported." in payment["failure_reason"]


def test_capture_requires_approved_order(fresh):
    fake = FakePayPalClient(order_status="CREATED")
    created = _create(fake)

    with pytest.raises(sandbox.SandboxError) as exc:
        sandbox.capture_test_order(created["order_id"], client=fake)

    assert exc.value.status_code == 409
    assert not any(call[0] == "capture_order" for call in fake.calls)
    payment = db.query_one(
        "SELECT * FROM payments WHERE id = ?", (created["payment_id"],)
    )
    assert payment["status"] == "pending"
    assert payment["paypal_capture_id"] is None


def test_capture_success_persists_capture_id(fresh):
    fake = FakePayPalClient()
    created = _create(fake)

    result = sandbox.capture_test_order(created["order_id"], client=fake)

    assert result["status"] == "succeeded"
    assert result["paypal_capture_id"] == "CAP-123"
    payment = db.query_one(
        "SELECT * FROM payments WHERE id = ?", (created["payment_id"],)
    )
    order = db.query_one(
        "SELECT * FROM orders WHERE id = ?", (created["order_id"],)
    )
    assert payment["status"] == "succeeded"
    assert payment["paypal_capture_id"] == "CAP-123"
    assert payment["paypal_order_id"] == "PP-ORDER-1"
    assert order["status"] == "fulfilled"

    capture_call = [c for c in fake.calls if c[0] == "capture_order"][0]
    assert capture_call[2] == f"paypilot-capture-{created['order_id']}"


def test_capture_without_completed_capture_does_not_mark_succeeded(fresh):
    fake = FakePayPalClient(capture_status="PENDING")
    created = _create(fake)

    with pytest.raises(sandbox.SandboxError) as exc:
        sandbox.capture_test_order(created["order_id"], client=fake)

    assert exc.value.status_code == 502
    payment = db.query_one(
        "SELECT * FROM payments WHERE id = ?", (created["payment_id"],)
    )
    assert payment["status"] == "pending"
    assert payment["paypal_capture_id"] is None


def test_capture_is_idempotent(fresh):
    fake = FakePayPalClient()
    created = _create(fake)

    first = sandbox.capture_test_order(created["order_id"], client=fake)
    calls_after_first = len(fake.calls)
    second = sandbox.capture_test_order(created["order_id"], client=fake)

    assert second["paypal_capture_id"] == first["paypal_capture_id"]
    assert second["mode"] == "already_captured"
    assert len(fake.calls) == calls_after_first


def test_mock_adapter_checkout_methods_offline():
    client = PayPilotClient()
    assert client.live is False

    created = client.create_order(1234, request_id="r1", reference_id="O-1")
    assert created["ok"] is True
    assert created["status"] == "CREATED"
    assert any(link["rel"] == "payer-action" for link in created["links"])

    order_id = created["id"]
    assert client.get_order(order_id)["status"] == "APPROVED"

    captured = client.capture_order(order_id, request_id="r2")
    assert captured["ok"] is True
    assert captured["status"] == "COMPLETED"
    assert (
        captured["purchase_units"][0]["payments"]["captures"][0]["status"]
        == "COMPLETED"
    )


def test_live_adapter_sends_intent_and_request_ids(monkeypatch):
    monkeypatch.setattr(
        payment_client,
        "settings",
        SimpleNamespace(
            paypal_live=True,
            paypal_api_base=SANDBOX_API_BASE,
            paypal_client_id="test-id",
            paypal_client_secret="test-secret",
        ),
    )
    client = PayPilotClient()
    monkeypatch.setattr(client, "_access_token", lambda: "fake-token")

    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        if url.endswith("/capture"):
            return httpx.Response(200, json={
                "id": "PP-ORDER-1",
                "status": "COMPLETED",
                "purchase_units": [{
                    "payments": {
                        "captures": [{"id": "CAP-1", "status": "COMPLETED"}]
                    }
                }],
            })
        return httpx.Response(200, json={
            "id": "PP-ORDER-1",
            "status": "CREATED",
            "links": [{
                "rel": "payer-action",
                "href": "https://www.sandbox.paypal.com/x",
            }],
        })

    monkeypatch.setattr(payment_client.httpx, "post", fake_post)

    # Omit currency: the adapter's checkout default must be USD.
    created = client.create_order(
        1234, request_id="stable-create", reference_id="O-1"
    )
    captured = client.capture_order("PP-ORDER-1", request_id="stable-capture")

    assert created["ok"] is True and captured["ok"] is True
    create_url, create_kwargs = posts[0]
    assert create_url == f"{SANDBOX_API_BASE}/v2/checkout/orders"
    assert create_kwargs["json"]["intent"] == "CAPTURE"
    # USD uses two decimal minor units: 1234 cents -> 12.34.
    assert create_kwargs["json"]["purchase_units"][0]["amount"] == {
        "value": "12.34",
        "currency_code": "USD",
    }
    assert create_kwargs["json"]["purchase_units"][0]["reference_id"] == "O-1"
    assert create_kwargs["headers"]["PayPal-Request-Id"] == "stable-create"

    capture_url, capture_kwargs = posts[1]
    assert capture_url == f"{SANDBOX_API_BASE}/v2/checkout/orders/PP-ORDER-1/capture"
    assert capture_kwargs["headers"]["PayPal-Request-Id"] == "stable-capture"


def test_live_adapter_forwards_explicit_inr_override(monkeypatch):
    monkeypatch.setattr(
        payment_client,
        "settings",
        SimpleNamespace(
            paypal_live=True,
            paypal_api_base=SANDBOX_API_BASE,
            paypal_client_id="test-id",
            paypal_client_secret="test-secret",
        ),
    )
    client = PayPilotClient()
    monkeypatch.setattr(client, "_access_token", lambda: "fake-token")

    posts: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return httpx.Response(200, json={
            "id": "PP-INR-1",
            "status": "CREATED",
            "links": [{
                "rel": "payer-action",
                "href": "https://www.sandbox.paypal.com/x",
            }],
        })

    monkeypatch.setattr(payment_client.httpx, "post", fake_post)

    result = client.create_order(1234, "INR", request_id="inr-create")

    assert result["ok"] is True
    assert posts[0][1]["json"]["purchase_units"][0]["amount"] == {
        "value": "12.34",
        "currency_code": "INR",
    }


def test_live_adapter_exposes_paypal_validation_details(monkeypatch):
    monkeypatch.setattr(
        payment_client,
        "settings",
        SimpleNamespace(
            paypal_live=True,
            paypal_api_base=SANDBOX_API_BASE,
            paypal_client_id="test-id",
            paypal_client_secret="test-secret",
        ),
    )
    client = PayPilotClient()
    monkeypatch.setattr(client, "_access_token", lambda: "fake-token")

    paypal_error = {
        "name": "UNPROCESSABLE_ENTITY",
        "message": (
            "The requested action could not be performed, semantically "
            "incorrect, or failed business validation."
        ),
        "debug_id": "f240942d779aa",
        "details": [
            {
                "issue": "CURRENCY_NOT_SUPPORTED",
                "description": "Currency code is not supported for this operation.",
                "field": "/purchase_units/@reference_id=='default'/amount/currency_code",
                "location": "body",
                "value": "INR",
            },
            {
                "issue": "INVALID_PARAMETER_VALUE",
                "description": "The value of a parameter is invalid.",
            },
            "not-a-detail",
            {"unrelated": "ignored"},
        ],
        "links": [{"href": f"{SANDBOX_API_BASE}/v2/checkout/orders"}],
        "access_token": "must-not-leak",
        "client_secret": "must-not-leak",
    }
    monkeypatch.setattr(
        payment_client.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(422, json=paypal_error),
    )

    result = client.create_order(1234)

    assert result["ok"] is False
    error = result["error"]
    assert error["http_status"] == 422
    assert error["name"] == "UNPROCESSABLE_ENTITY"
    assert error["debug_id"] == "f240942d779aa"
    assert error["details"] == [
        {
            "issue": "CURRENCY_NOT_SUPPORTED",
            "description": "Currency code is not supported for this operation.",
        },
        {
            "issue": "INVALID_PARAMETER_VALUE",
            "description": "The value of a parameter is invalid.",
        },
    ]
    # Only issue/description survive; field/location/value and unknown keys do not.
    assert all(
        set(detail) <= {"issue", "description"} for detail in error["details"]
    )
    serialized = json.dumps(result)
    assert "/purchase_units" not in serialized
    assert "must-not-leak" not in serialized
    assert "access_token" not in serialized
    assert "client_secret" not in serialized


def test_live_client_refuses_non_sandbox_base(monkeypatch):
    monkeypatch.setattr(
        payment_client,
        "settings",
        SimpleNamespace(
            paypal_live=True,
            paypal_api_base="https://api-m.paypal.com",
            paypal_client_id="test-id",
            paypal_client_secret="test-secret",
        ),
    )

    with pytest.raises(RuntimeError, match="non-sandbox"):
        PayPilotClient()


def test_sandbox_endpoints_create_then_capture(fresh, monkeypatch):
    monkeypatch.setattr(sandbox, "PayPilotClient", lambda: FakePayPalClient())

    with TestClient(app) as api:
        # Omit currency: the API default must be USD.
        created = api.post("/api/sandbox/orders", json={"amount_cents": 1234})
        assert created.status_code == 201
        body = created.json()
        assert body["currency"] == "USD"
        assert body["approval_url"].startswith("https://www.sandbox.paypal.com/")
        assert "access_token" not in body
        assert "client_secret" not in body

        captured = api.post(f"/api/sandbox/orders/{body['order_id']}/capture")
        assert captured.status_code == 200
        assert captured.json()["status"] == "succeeded"
        assert captured.json()["paypal_capture_id"] == "CAP-123"

    payment = db.query_one(
        "SELECT * FROM payments WHERE id = ?", (body["payment_id"],)
    )
    assert payment["status"] == "succeeded"
    assert payment["paypal_capture_id"] == "CAP-123"
