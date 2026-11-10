"""Application configuration loaded from environment variables.

Centralizes runtime settings (database path, planner mode, PayPal mode,
and safety limits) so modules share a single source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv is convenient but must not be a hard dependency
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Sandbox checkout currency. This is the single default for the create-order
# flow. It is a default, not a restriction: callers may still pass an explicit
# ISO code (for example "INR") and it is forwarded to PayPal unchanged. Seeded
# demo rows and the DB defaults remain USD.
DEFAULT_CHECKOUT_CURRENCY = "USD"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    anthropic_model: str
    gemini_api_key: str | None
    gemini_model: str
    paypal_mode: str            # "mock" | "live"
    paypal_client_id: str | None
    paypal_client_secret: str | None
    paypal_api_base: str
    db_path: str
    max_agent_steps: int

    @property
    def planner_provider(self) -> str:
        """Selected planner provider: gemini, anthropic, or rules."""
        if self.gemini_api_key:
            return "gemini"
        if self.anthropic_api_key:
            return "anthropic"
        return "rules"

    @property
    def planner_model(self) -> str | None:
        """Exact planner model/config name for the selected provider (non-sensitive)."""
        provider = self.planner_provider
        if provider == "gemini":
            return self.gemini_model
        if provider == "anthropic":
            return self.anthropic_model
        return None

    @property
    def llm_enabled(self) -> bool:
        return self.planner_provider != "rules"

    @property
    def paypal_live(self) -> bool:
        return (
            self.paypal_mode == "live"
            and bool(self.paypal_client_id)
            and bool(self.paypal_client_secret)
        )


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        paypal_mode=os.getenv("PAYPAL_MODE", "mock").strip().lower(),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID") or None,
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET") or None,
        paypal_api_base=os.getenv(
            "PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com"
        ).rstrip("/"),
        db_path=os.getenv("PAYPILOT_DB", "paypilot.db"),
        max_agent_steps=int(os.getenv("MAX_AGENT_STEPS", "12")),
    )


settings = load_settings()
