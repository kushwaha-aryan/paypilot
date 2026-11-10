# PayPilot

PayPilot is a payment operations console for investigating and resolving payment incidents with a controlled agent workflow. It combines evidence collection, a deterministic policy gate, and a hash-chained audit ledger so automated actions are explainable and bounded.

Built for the PayPal AI Hackathon (sandbox-only).

## Overview

Payment operations often require fast decisions under risk: failed payments may need a retry, suspicious charges need review, and refund requests must respect merchant policy. PayPilot models these cases as incidents and runs an agent loop that gathers evidence, proposes an action, and only executes actions the policy engine allows (or a human explicitly approves).

## Key features

- **Incident queue and detail view** for payment failures, suspicious charges, refund requests, and duplicate charges.
- **Evidence-first agent run**: read tools collect payment, order, customer, history, risk, and policy evidence; each call is recorded.
- **Deterministic policy gate** (`allow` / `review` / `deny`) that decides whether a proposed action can execute, needs human approval, or must be blocked.
- **PayPal Sandbox integration** for checkout order creation/capture, refund execution (when eligible), and read-only refund lookup.
- **Hash-chained audit ledger** with verification endpoint to detect tampering.
- **Operations UI**: dashboard, architecture view, and interactive API docs (Swagger UI over `/openapi.json`).

## Architecture

```text
PayPal Sandbox / API requests
            |
            v
     FastAPI endpoints
            |
            v
  Incident + evidence data (SQLite)
            |
            v
        Agent loop
   (planner + tool registry)
            |
            v
    Deterministic policy gate
      allow / review / deny
            |
      +-----+------+
      |            |
  auto-execute   human review
      |            |
      v            v
 PayPal adapter   approve/reject
      \            /
       \          /
        v        v
      Hash-chained ledger
```

Primary modules:

- `backend/app/main.py` — HTTP API and static page routes
- `backend/app/agent/loop.py` — agent run loop and trace
- `backend/app/agent/brain.py` — planners (deterministic rules + optional LLM)
- `backend/app/agent/tools.py` — read/action tools, evidence recording, execution
- `backend/app/policy.py` — deterministic policy decisions
- `backend/app/payment_client.py` — PayPal adapter (mock or live sandbox)
- `backend/app/sandbox.py` — sandbox checkout create/capture flow
- `backend/app/incidents.py` — refund incident intake
- `backend/app/db.py` — SQLite schema, queries, hash-chained ledger
- `backend/app/seed.py` — deterministic demo dataset
- `backend/app/static/` — dashboard, architecture, docs, shared CSS

## How the agent works

1. **Plan the next step.** A planner selects a tool call based on incident context and prior trace.
2. **Collect evidence.** Read tools (`get_payment`, `get_order`, `get_customer`, `get_customer_history`, `check_risk`, `search_policy`) append evidence rows and ledger entries.
3. **Propose an action.** The agent proposes an action (e.g. `retry_payment`, `refund`, `flag_for_review`, `request_confirmation`, `close_incident`) with cited evidence IDs.
4. **Evaluate policy.** `policy.py` returns `allow`, `review`, or `deny` using deterministic rules (no model call).
5. **Execute or pause.** `allow` actions execute immediately through the tool registry; `review` actions wait for human approval; `deny` actions are blocked.
6. **Audit everything.** Tool calls, proposals, verdicts, approvals/rejections, and outcomes are appended to the hash-chained ledger.

### AI/agent technology (exact)

- **Default planner: deterministic rule-based planner (`RulePlanner`)** — no network calls, no model dependency. This is the default when no LLM API key is configured and is the behavior exercised by the automated tests.
- **Optional LLM planners (provider-selected):**
  - **Gemini (`GeminiPlanner`)** — when `GEMINI_API_KEY` is set, the planner can request next-step decisions via the Google Gen AI Python SDK using `GEMINI_MODEL` (default `gemini-3.1-flash-lite`).
  - **Anthropic (`AnthropicPlanner`)** — when `ANTHROPIC_API_KEY` is set (and no Gemini key is configured), the planner can request next-step decisions via Anthropic’s Messages API using `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).
  - If both Gemini and Anthropic keys are configured, **Gemini is selected**. Model outputs are parsed/validated into structured tool proposals before entering the normal execution path.
- **Policy gate:** always deterministic Python (`policy.py`). LLM planners propose tool calls only; they do **not** execute payment actions directly and cannot bypass the tool registry or policy gate. Final authority over execution is enforced in code.

## PayPal Sandbox integration

- **Modes:** `PAYPAL_MODE=mock` (offline, deterministic responses) or `PAYPAL_MODE=live` (PayPal Sandbox REST API with OAuth client credentials). Live mode is only enabled when sandbox credentials are present and the API base is the sandbox host.
- **Checkout flow:** `POST /api/sandbox/orders` creates a sandbox order and returns an approval URL; `POST /api/sandbox/orders/{order_id}/capture` verifies approval and captures a completed payment.
- **Refund execution:** eligible refund actions call the PayPal refund API (or mock) through the adapter; missing capture IDs and failed calls are handled as non-destructive failures.
- **Live refund lookup (read-only):** `GET /api/paypal/refunds/{refund_id}` performs a GET against the PayPal Sandbox refund-details endpoint for a refund ID already recorded in the system, returning status/amount/timestamps for verification in the UI. This endpoint does not create or modify payments, captures, orders, or refunds.

## Tech stack

- **Backend:** Python, FastAPI, Pydantic, SQLite, httpx
- **Agent/planning:** custom rule-based planner; optional Google Gen AI SDK (Gemini) and Anthropic SDK planners
- **Payments:** PayPal Sandbox REST APIs (Orders, Captures, Refunds)
- **Frontend:** static HTML/CSS/JavaScript served by FastAPI
- **Testing:** pytest + FastAPI TestClient

## Prerequisites

- Python 3.11+ (3.13 tested in development)
- Windows PowerShell (instructions below) or a POSIX shell
- Optional (for live sandbox): PayPal Developer sandbox REST app credentials
- Optional (for LLM planner): Gemini API key and/or Anthropic API key

## Setup (Windows)

1. **Clone and enter the repository**

   ```powershell
   git clone https://github.com/kushwaha-aryan/paypilot.git
   cd paypilot
   ```

2. **Create a virtual environment and install dependencies**

   ```powershell
   cd backend
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   Copy the example file and edit values as needed:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `backend/.env` (do not commit it). See [Environment variables](#environment-variables).

4. **Seed the demo dataset (optional but recommended for first run)**

   ```powershell
   python -m app.seed
   ```

   Seeding resets the database tables and inserts the deterministic demo incidents/payments.

## Environment variables

Defined in `backend/.env` (loaded on startup). All are optional for mock/offline use.

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(empty)* | Enables the Gemini planner when set; leave empty to use other planners/fallback. |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model ID used by the optional Gemini planner. |
| `ANTHROPIC_API_KEY` | *(empty)* | Enables the Anthropic planner when set (used if no Gemini key is configured). |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Anthropic model ID used by the optional Anthropic planner. |
| `PAYPAL_MODE` | `mock` | `mock` for offline deterministic responses, `live` for PayPal Sandbox (requires credentials). |
| `PAYPAL_CLIENT_ID` | *(empty)* | PayPal Sandbox REST app client ID (live mode). |
| `PAYPAL_CLIENT_SECRET` | *(empty)* | PayPal Sandbox REST app secret (live mode). |
| `PAYPAL_API_BASE` | `https://api-m.sandbox.paypal.com` | PayPal API base URL (sandbox). |
| `PAYPILOT_DB` | `paypilot.db` | SQLite database path (resolved relative to the backend working directory when run as shown). |
| `MAX_AGENT_STEPS` | `12` | Maximum planner/tool steps per agent run. |

> Planner selection: `GEMINI_API_KEY` → Gemini, else `ANTHROPIC_API_KEY` → Anthropic, else deterministic rules. Model planners are optional; the deterministic policy gate remains the final authority.

Use `backend/.env.example` as the placeholder template. Never commit real credentials.

## Run the backend

From the `backend` directory with the virtual environment activated:

```powershell
uvicorn app.main:app --reload
```

Default URL: **http://127.0.0.1:8000**

- **Dashboard:** http://127.0.0.1:8000/
- **Architecture:** http://127.0.0.1:8000/architecture
- **API docs (interactive):** http://127.0.0.1:8000/docs
- **OpenAPI schema:** http://127.0.0.1:8000/openapi.json
- **Health:** http://127.0.0.1:8000/api/health

> The interactive docs page loads Swagger UI assets from a CDN; if unavailable, the raw schema remains accessible at `/openapi.json`.

## Run tests

From the `backend` directory:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The test suite runs in isolated temporary databases with PayPal in mock mode and no LLM key.

## Sandbox demo and safety limitations

- **Sandbox-only:** live PayPal integration is restricted to the PayPal Sandbox API base. Do not use production credentials.
- **No real money:** all payment operations are against sandbox/mock environments.
- **Deterministic policy controls:** action execution is gated by `policy.py`; some actions require human approval and denied actions cannot be force-executed.
- **Idempotency/guards:** refund incident creation is idempotent per payment in open/awaiting states; executed actions are not re-executed; live refunds require a stored PayPal capture ID.
- **Read-only proof:** the live refund lookup endpoint only reads an existing, system-known refund ID.
- **Demo caveat:** running `python -m app.seed` resets the database to the seeded demo state.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Please keep changes focused, avoid committing secrets or local data, and run the test suite before submitting.
