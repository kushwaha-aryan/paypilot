"""SQLite storage + the hash-chained audit ledger.

Why SQLite: the whole dataset is thousands of rows, it ships as one file, and a
judge can open it. Postgres buys nothing at this scale and costs a setup step.

Why the ledger is hash-chained: an audit log that anyone can edit is decoration.
Each entry stores hash(previous_hash + entry fields), so tampering with any
earlier row breaks every later hash. `verify_ledger` walks the chain and reports
the first break. That is a five-line idea that makes the whole trust story
checkable rather than claimed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS merchants (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    refund_auto_limit_cents INTEGER NOT NULL,
    review_timeout_hours    INTEGER NOT NULL DEFAULT 24
);

CREATE TABLE IF NOT EXISTS customers (
    id                  TEXT PRIMARY KEY,
    merchant_id         TEXT NOT NULL REFERENCES merchants(id),
    name                TEXT NOT NULL,
    email               TEXT NOT NULL,
    account_created_at  TEXT NOT NULL,
    lifetime_orders     INTEGER NOT NULL DEFAULT 0,
    lifetime_spend_cents INTEGER NOT NULL DEFAULT 0,
    country_iso         TEXT NOT NULL,
    known_device_ids    TEXT NOT NULL DEFAULT '[]'   -- JSON array
);

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,          -- internal id (stable, used by FKs/UI)
    paypal_order_id   TEXT,                      -- external PayPal order id
    merchant_id       TEXT NOT NULL REFERENCES merchants(id),
    customer_id       TEXT NOT NULL REFERENCES customers(id),
    amount_cents      INTEGER NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL              -- created|fulfilled|refunded|disputed
);

CREATE TABLE IF NOT EXISTS payments (
    id                  TEXT PRIMARY KEY,        -- internal id (stable, used by FKs/UI)
    paypal_capture_id   TEXT,                    -- external PayPal capture id
    paypal_order_id     TEXT,                    -- external PayPal order id
    order_id            TEXT NOT NULL REFERENCES orders(id),
    amount_cents        INTEGER NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'USD',
    status              TEXT NOT NULL,           -- succeeded|failed|refunded|pending
    failure_reason      TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 1,
    risk_score          REAL NOT NULL DEFAULT 0.0,
    ip_country          TEXT,
    device_id           TEXT,
    created_at          TEXT NOT NULL
);

-- One row per refund attempt. The internal payment id stays the foreign key;
-- PayPal's own identifiers live in their own columns so mock data and live
-- sandbox data can coexist without rewriting internal references.
CREATE TABLE IF NOT EXISTS refunds (
    id                TEXT PRIMARY KEY,
    payment_id        TEXT NOT NULL REFERENCES payments(id),
    capture_id        TEXT,                      -- PayPal capture being refunded
    paypal_refund_id  TEXT,                      -- PayPal refund id
    amount_cents      INTEGER NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',
    status            TEXT NOT NULL,             -- pending|completed|failed|cancelled
    idempotency_key   TEXT,
    created_at        TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_idempotency_key
    ON refunds(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_paypal_refund_id
    ON refunds(paypal_refund_id);

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    merchant_id     TEXT NOT NULL REFERENCES merchants(id),
    order_id        TEXT NOT NULL REFERENCES orders(id),
    payment_id      TEXT NOT NULL REFERENCES payments(id),
    kind            TEXT NOT NULL,   -- payment_failed|refund_request|suspicious_charge|duplicate_charge
    status          TEXT NOT NULL,   -- open|awaiting_approval|resolved|escalated
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    outcome         TEXT             -- recovered|refunded|flagged|confirmed_legit|abandoned
);

-- The agent's evidence ledger. Every read tool appends here, and every claim
-- the agent makes must point at an evidence id.
CREATE TABLE IF NOT EXISTS evidence (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    tool        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    payload     TEXT NOT NULL,   -- JSON
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id            TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(id),
    type          TEXT NOT NULL,   -- retry_payment|refund|flag_for_review|request_confirmation|close_incident
    params        TEXT NOT NULL,   -- JSON
    evidence_ids  TEXT NOT NULL,   -- JSON array; empty => policy rejects
    policy_verdict TEXT NOT NULL,  -- allow|review|deny
    policy_reason TEXT NOT NULL,
    status        TEXT NOT NULL,   -- proposed|executed|failed|rejected|awaiting_approval
    executed_at   TEXT,
    result        TEXT             -- JSON
);

CREATE TABLE IF NOT EXISTS ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- incident_opened|tool_call|action_proposed|...
    incident_id TEXT,
    payload     TEXT NOT NULL,     -- JSON
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns introduced after the first release. init_db() adds these in place, so
# an existing database keeps every row instead of being deleted and reseeded.
# Never put destructive changes here.
ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (("paypal_order_id", "TEXT"),),
    "payments": (
        ("paypal_capture_id", "TEXT"),
        ("paypal_order_id", "TEXT"),
    ),
}


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue  # table not created yet; SCHEMA handles it
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                applied.append(f"{table}.{name}")
    return applied


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> list[str]:
    """Create missing tables and add missing columns.

    Additive only: existing rows, internal ids and foreign keys are never
    dropped or rewritten. Returns the columns that had to be added.
    """
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        return _add_missing_columns(conn)


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with get_conn() as conn:
        conn.execute(sql, params)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

GENESIS = "0" * 64


def _freeze(payload: dict[str, Any]) -> str:
    """Canonical JSON so the same payload always hashes identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def append_ledger(
    kind: str, payload: dict[str, Any], incident_id: str | None = None
) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["hash"] if row else GENESIS

        body = _freeze(payload)
        ts = utcnow()
        digest = hashlib.sha256(
            f"{prev_hash}|{ts}|{kind}|{incident_id or ''}|{body}".encode()
        ).hexdigest()

        cur = conn.execute(
            "INSERT INTO ledger (ts, kind, incident_id, payload, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, kind, incident_id, body, prev_hash, digest),
        )
        return {"seq": cur.lastrowid, "hash": digest, "prev_hash": prev_hash}


def verify_ledger() -> dict[str, Any]:
    """Recompute the whole chain. Returns ok + first broken sequence number."""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ledger ORDER BY seq")]
    prev_hash = GENESIS
    for row in rows:
        expected = hashlib.sha256(
            f"{prev_hash}|{row['ts']}|{row['kind']}|{row['incident_id'] or ''}|"
            f"{row['payload']}".encode()
        ).hexdigest()
        if expected != row["hash"]:
            return {
                "ok": False,
                "broken_at_seq": row["seq"],
                "entries": len(rows),
                "message": "ledger hash chain broken — entries may have been edited",
            }
        prev_hash = row["hash"]
    return {"ok": True, "entries": len(rows), "message": "chain intact"}
