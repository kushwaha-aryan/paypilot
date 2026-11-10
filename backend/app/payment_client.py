"""PayPal Sandbox adapter.

Provides a single interface with two modes:
- mock: deterministic responses with no network access (default)
- live: PayPal Sandbox REST API via OAuth2 client credentials

The agent uses this adapter without depending on the active mode.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx

from .config import DEFAULT_CHECKOUT_CURRENCY, settings

SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"


def _money(amount_cents: int) -> str:
    """Exact decimal string for PayPal, without float rounding."""
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


_MAX_ERROR_TEXT = 500
_MAX_ERROR_DETAILS = 20


def _error_text(value: Any) -> str | None:
    """Collapse whitespace and cap length so no raw payload is echoed."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:_MAX_ERROR_TEXT] if text else None


def _safe_details(body: dict[str, Any]) -> list[dict[str, str]]:
    """PayPal's actionable validation reasons, reduced to issue/description."""
    raw = body.get("details")
    if not isinstance(raw, list):
        return []
    details: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        issue = _error_text(item.get("issue"))
        description = _error_text(item.get("description"))
        if not issue and not description:
            continue
        detail: dict[str, str] = {}
        if issue:
            detail["issue"] = issue
        if description:
            detail["description"] = description
        details.append(detail)
        if len(details) >= _MAX_ERROR_DETAILS:
            break
    return details


def _safe_error(resp: httpx.Response) -> dict[str, Any]:
    """Only the fields a human needs. Never echo headers, auth, or raw bodies.

    PayPal's top-level message for UNPROCESSABLE_ENTITY is generic; the actual
    validation reason lives in details[].issue / details[].description. Keep
    only those two fields, whitespace-collapsed and length-capped.
    """
    try:
        body = resp.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {
        "http_status": resp.status_code,
        "name": _error_text(body.get("name")) or _error_text(body.get("error")),
        "message": _error_text(body.get("message"))
        or _error_text(body.get("error_description")),
        "debug_id": _error_text(body.get("debug_id"))
        or _error_text(resp.headers.get("paypal-debug-id")),
        "details": _safe_details(body),
    }


class PayPilotClient:
    def __init__(self) -> None:
        self.live = settings.paypal_live
        self.base = settings.paypal_api_base
        self._token: str | None = None
        if self.live and self.base != SANDBOX_API_BASE:
            raise RuntimeError(
                "PayPilot refuses to call live PayPal against a non-sandbox "
                f"API base ({self.base!r})"
            )

    # -- auth --------------------------------------------------------------
    def _access_token(self) -> str:
        if self.base != SANDBOX_API_BASE:
            raise RuntimeError(
                "PayPilot only permits the PayPal Sandbox API base URL "
                f"({SANDBOX_API_BASE!r})"
            )
        if self._token:
            return self._token
        resp = httpx.post(
            f"{self.base}/v1/oauth2/token",
            auth=(settings.paypal_client_id or "", settings.paypal_client_secret or ""),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        return headers

    def _id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"

    # -- checkout ----------------------------------------------------------
    def create_order(
        self,
        amount_cents: int,
        currency: str = DEFAULT_CHECKOUT_CURRENCY,
        *,
        request_id: str | None = None,
        reference_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an intent=CAPTURE order and return PayPal's links/approval URL."""
        if not self.live:
            order_id = self._id("ORDER")
            approval = f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
            return {
                "ok": True,
                "mode": "mock",
                "id": order_id,
                "status": "CREATED",
                "links": [
                    {"href": approval, "rel": "payer-action", "method": "GET"},
                    {"href": approval, "rel": "approve", "method": "GET"},
                ],
            }
        resp = httpx.post(
            f"{self.base}/v2/checkout/orders",
            headers=self._headers(request_id),
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": reference_id or "default",
                    "amount": {
                        "value": _money(amount_cents),
                        "currency_code": currency,
                    },
                }],
            },
            timeout=20,
        )
        if not resp.is_success:
            return {"ok": False, "mode": "live", "error": _safe_error(resp)}
        return {"ok": True, "mode": "live", **resp.json()}

    def capture_order(
        self, order_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        """Capture an approved order. The caller must still verify the response."""
        if not self.live:
            return {
                "ok": True,
                "mode": "mock",
                "id": order_id,
                "status": "COMPLETED",
                "purchase_units": [{
                    "reference_id": "default",
                    "payments": {
                        "captures": [{
                            "id": self._id("CAPTURE"),
                            "status": "COMPLETED",
                        }],
                    },
                }],
            }
        resp = httpx.post(
            f"{self.base}/v2/checkout/orders/{order_id}/capture",
            headers=self._headers(request_id),
            json={},
            timeout=20,
        )
        if not resp.is_success:
            return {"ok": False, "mode": "live", "error": _safe_error(resp)}
        return {"ok": True, "mode": "live", **resp.json()}

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Read the order so capture can require status=APPROVED first."""
        if not self.live:
            return {
                "ok": True,
                "mode": "mock",
                "id": order_id,
                "status": "APPROVED",
                "links": [],
            }
        resp = httpx.get(
            f"{self.base}/v2/checkout/orders/{order_id}",
            headers=self._headers(),
            timeout=20,
        )
        if not resp.is_success:
            return {"ok": False, "mode": "live", "error": _safe_error(resp)}
        return {"ok": True, "mode": "live", **resp.json()}

    # -- operations --------------------------------------------------------
    def refund(
        self,
        capture_id: str,
        amount_cents: int,
        currency: str = "USD",
        note: str = "",
    ) -> dict[str, Any]:
        if not self.live:
            return {
                "ok": True,
                "mode": "mock",
                "refund_id": self._id("REFUND"),
                "capture_id": capture_id,
                "amount": {"value": _money(amount_cents), "currency_code": currency},
                "note": note,
            }
        try:
            resp = httpx.post(
                f"{self.base}/v2/payments/captures/{capture_id}/refund",
                headers=self._headers(),
                json={
                    "amount": {"value": _money(amount_cents),
                               "currency_code": currency},
                    "note_to_payer": note,
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            # Auth/network failures must be a result, not an exception that
            # escapes through the agent loop into the HTTP endpoint.
            return {
                "ok": False,
                "mode": "live",
                "error": {
                    "name": type(exc).__name__,
                    "message": "PayPal request failed",
                },
            }
        if not resp.is_success:
            return {"ok": False, "mode": "live", "error": _safe_error(resp)}
        return {"ok": True, "mode": "live", **resp.json()}

    def get_refund(self, refund_id: str) -> dict[str, Any]:
        """Read an existing refund. Pure GET: nothing is created or moved.

        Used by the dashboard's live proof tile, so failures are returned as a
        result (like ``refund``) instead of escaping into the HTTP layer.
        """
        if not self.live:
            return {
                "ok": True,
                "mode": "mock",
                "id": refund_id,
                "status": "COMPLETED",
                "amount": {"value": "0.00", "currency_code": "USD"},
                "create_time": None,
                "note_to_payer": "customer refund request",
                "links": [],
            }
        try:
            resp = httpx.get(
                f"{self.base}/v2/payments/refunds/{refund_id}",
                headers=self._headers(),
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "mode": "live",
                "error": {
                    "name": type(exc).__name__,
                    "message": "PayPal request failed",
                },
            }
        if not resp.is_success:
            return {"ok": False, "mode": "live", "error": _safe_error(resp)}
        return {"ok": True, "mode": "live", **resp.json()}

    def retry_order(
        self, order_id: str, amount_cents: int, currency: str = "USD"
    ) -> dict[str, Any]:
        """Create a fresh order for the same amount (PayPal has no 'retry' verb)."""
        if not self.live:
            return {
                "ok": True,
                "mode": "mock",
                "new_order_id": self._id("ORDER"),
                "previous_order_id": order_id,
                "amount": {"value": _money(amount_cents), "currency_code": currency},
            }
        resp = httpx.post(
            f"{self.base}/v2/checkout/orders",
            headers=self._headers(),
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": order_id,
                    "amount": {
                        "value": _money(amount_cents),
                        "currency_code": currency,
                    },
                }],
            },
            timeout=20,
        )
        resp.raise_for_status()
        return {"ok": True, "mode": "live", **resp.json()}
