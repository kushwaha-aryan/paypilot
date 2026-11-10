"""Harmless Gemini integration smoke test for PayPilot.

- Uses project configuration (GEMINI_API_KEY/GEMINI_MODEL from environment/.env).
- Retries the basic Gemini call up to 3 times (5s pause, 60s client timeout).
- On basic success, tests GeminiPlanner.next_action([]) for real parseability.
- Never prints or logs API keys; never executes tools/payments/refunds or mutates DB.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import config
from app.agent.brain import GeminiPlanner, Plan


class _FakeRegistry:
    incident_id = "SMOKE-INC"

    def describe_incident(self) -> str:
        return (
            "Incident SMOKE-INC (payment_failed) for order O-1001 from customer Maya Patel. "
            "Payment P-1001 is failed with reason INSUFFICIENT_FUNDS, amount 4500 USD."
        )


def _safe_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    msg = str(exc)
    key = config.settings.gemini_api_key or ""
    if key:
        msg = msg.replace(key, "[REDACTED]")
    msg = " ".join(msg.split())
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return f"{name}: {msg}" if msg else name


def run_basic_attempt(attempt: int, attempts: int) -> tuple[bool, str | None, str | None]:
    from google import genai
    from google.genai import types

    model = config.settings.gemini_model
    client = None
    try:
        client = genai.Client(
            api_key=config.settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=60000),
        )
        resp = client.models.generate_content(
            model=model,
            contents="Reply with exactly: OK",
        )
        text = (getattr(resp, "text", "") or "").strip()
        ok = text == "OK"
        detail = f"response={text!r}" if ok else f"unexpected response={text!r}"
        return ok, detail, None
    except Exception as exc:  # noqa: BLE001
        return False, None, _safe_error(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def run_basic_with_retries(attempts: int = 3, pause_seconds: int = 5) -> bool:
    for attempt in range(1, attempts + 1):
        ok, detail, err = run_basic_attempt(attempt, attempts)
        if ok:
            print(f"basic_attempt_{attempt}: SUCCESS ({detail})")
            return True
        if err:
            print(f"basic_attempt_{attempt}: FAILED ({err})")
        else:
            print(f"basic_attempt_{attempt}: FAILED ({detail})")
        if attempt < attempts:
            time.sleep(pause_seconds)
    return False


def run_planner_parse_check() -> tuple[bool, str]:
    planner = GeminiPlanner(_FakeRegistry())
    plan = planner.next_action([])
    if plan is None:
        return True, "planner_parse: SUCCESS (done/None)"
    if not isinstance(plan, Plan):
        return False, f"planner_parse: FAILED (unexpected result type {type(plan)!r})"

    # Distinguish real parseable planner output from integration failure sentinels.
    failure_sentinels = {
        "Model request failed; stopping.",
        "Model returned unparseable output; stopping.",
        "Model returned empty output; stopping.",
        "Model returned invalid output; stopping.",
        "Model proposed an unsupported tool; stopping.",
        "Model returned invalid arguments; stopping.",
        "Model returned an invalid action proposal; stopping.",
        "Model returned invalid action params; stopping.",
        "Model returned invalid evidence ids; stopping.",
    }
    if plan.tool == "stop" and plan.thought in failure_sentinels:
        return False, f"planner_parse: FAILED (tool='stop', thought={plan.thought!r})"

    return True, (
        f"planner_parse: SUCCESS (tool={plan.tool!r}, "
        f"thought={plan.thought!r}, args_keys={sorted(plan.args.keys())})"
    )


def main() -> int:
    model = config.settings.gemini_model
    provider = config.settings.planner_provider

    if not config.settings.gemini_api_key:
        print("Gemini API key is not configured in the environment.")
        return 2
    if provider != "gemini":
        print(f"Gemini planner is not selected (provider={provider}).")
        return 2

    print(f"runtime_planner_provider: {provider}")
    print(f"configured_model: {model}")
    print("basic_prompt: Reply with exactly: OK")
    print("basic_timeout_seconds: 60")
    print("basic_attempts: 3 (5s pause between attempts)")

    basic_ok = run_basic_with_retries(attempts=3, pause_seconds=5)
    if not basic_ok:
        print("overall: FAILED (basic Gemini prompt did not succeed after 3 attempts)")
        return 1

    try:
        parsed_ok, detail = run_planner_parse_check()
        print(detail)
    except Exception as exc:  # noqa: BLE001
        print(f"planner_parse: FAILED ({_safe_error(exc)})")
        return 1

    if not parsed_ok:
        print("overall: FAILED (planner response was not successfully parsed)")
        return 1

    print("overall: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
