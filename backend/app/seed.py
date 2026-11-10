"""Demo dataset.

Six incidents chosen so that every branch of the policy engine is exercised and
each one is visually distinct in the demo:

  INC-001  failed payment, low risk        -> confirm, retry, recovered
  INC-002  suspicious charge, risk 0.85    -> flagged automatically
  INC-003  refund above merchant limit     -> paused for human approval
  INC-004  duplicate charge, small amount  -> refunded automatically
  INC-005  failed payment, 3 attempts used -> stops, no retry
  INC-006  refund under limit, 2nd merchant-> refunded automatically

Everything is deterministic. Re-running resets the database.
"""
from __future__ import annotations

import json

from . import db

MERCHANTS = [
    ("M-1", "Northwind Supply", 5000, 24),    # $50 auto-refund cap
    ("M-2", "Lumen Goods", 15000, 24),        # $150 auto-refund cap
]

CUSTOMERS = [
    # id, merchant, name, email, created, orders, spend_cents, country, devices
    ("C-1", "M-1", "Maya Patel", "maya@example.com", "2024-01-10T00:00:00+00:00",
     12, 48000, "US", ["DEV-A", "DEV-B"]),
    ("C-2", "M-1", "Tom Reece", "tom@example.com", "2026-09-25T00:00:00+00:00",
     3, 9000, "US", ["DEV-X"]),
    ("C-3", "M-1", "Priya Shah", "priya@example.com", "2023-05-05T00:00:00+00:00",
     30, 90000, "IN", ["DEV-P1"]),
    ("C-4", "M-2", "Alex Kim", "alex@example.com", "2025-02-02T00:00:00+00:00",
     8, 40000, "US", ["DEV-K1"]),
]

ORDERS = [
    # id, merchant, customer, amount, created, status
    ("O-1001", "M-1", "C-1", 4500, "2026-10-18T14:00:00+00:00", "created"),
    ("O-1002", "M-1", "C-1", 2500, "2026-10-17T10:00:00+00:00", "fulfilled"),
    ("O-1003", "M-1", "C-2", 9900, "2026-10-18T03:12:00+00:00", "created"),
    ("O-1004", "M-1", "C-3", 12000, "2026-10-15T09:00:00+00:00", "fulfilled"),
    ("O-1005", "M-2", "C-4", 7600, "2026-10-16T16:30:00+00:00", "created"),
    ("O-1006", "M-2", "C-4", 9000, "2026-10-12T11:00:00+00:00", "fulfilled"),
]

PAYMENTS = [
    # id, order, amount, status, reason, attempts, risk, ip_country, device, created
    ("P-1001", "O-1001", 4500, "failed", "INSUFFICIENT_FUNDS", 1, 0.0,
     "US", "DEV-A", "2026-10-18T14:00:00+00:00"),
    ("P-1002", "O-1003", 9900, "succeeded", None, 1, 0.85,
     "CA", "DEV-NEW", "2026-10-18T03:12:00+00:00"),
    ("P-1003", "O-1004", 12000, "succeeded", None, 1, 0.0,
     "IN", "DEV-P1", "2026-10-15T09:00:00+00:00"),
    ("P-1004", "O-1002", 2500, "succeeded", None, 1, 0.0,
     "US", "DEV-A", "2026-10-17T10:00:00+00:00"),
    ("P-1005", "O-1005", 7600, "failed", "CARD_DECLINED", 3, 0.0,
     "US", "DEV-K1", "2026-10-16T16:30:00+00:00"),
    ("P-1006", "O-1006", 9000, "succeeded", None, 1, 0.0,
     "US", "DEV-K1", "2026-10-12T11:00:00+00:00"),
]

INCIDENTS = [
    # id, merchant, order, payment, kind, status, created
    ("INC-001", "M-1", "O-1001", "P-1001", "payment_failed", "open",
     "2026-10-18T14:01:00+00:00"),
    ("INC-002", "M-1", "O-1003", "P-1002", "suspicious_charge", "open",
     "2026-10-18T03:20:00+00:00"),
    ("INC-003", "M-1", "O-1004", "P-1003", "refund_request", "open",
     "2026-10-16T08:00:00+00:00"),
    ("INC-004", "M-1", "O-1002", "P-1004", "duplicate_charge", "open",
     "2026-10-17T12:00:00+00:00"),
    ("INC-005", "M-2", "O-1005", "P-1005", "payment_failed", "open",
     "2026-10-16T16:45:00+00:00"),
    ("INC-006", "M-2", "O-1006", "P-1006", "refund_request", "open",
     "2026-10-13T09:00:00+00:00"),
]


def seed() -> None:
    db.init_db()
    with db.get_conn() as conn:
        for table in ("evidence", "actions", "incidents", "refunds", "payments",
                      "orders", "customers", "merchants", "ledger"):
            conn.execute(f"DELETE FROM {table}")

        conn.executemany(
            "INSERT INTO merchants (id, name, refund_auto_limit_cents, "
            "review_timeout_hours) VALUES (?, ?, ?, ?)", MERCHANTS)

        conn.executemany(
            "INSERT INTO customers (id, merchant_id, name, email, account_created_at, "
            "lifetime_orders, lifetime_spend_cents, country_iso, known_device_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(cid, mid, name, email, created, orders, spend, country,
              json.dumps(devices))
             for cid, mid, name, email, created, orders, spend, country, devices
             in CUSTOMERS])

        conn.executemany(
            "INSERT INTO orders (id, merchant_id, customer_id, amount_cents, "
            "created_at, status) VALUES (?, ?, ?, ?, ?, ?)", ORDERS)

        conn.executemany(
            "INSERT INTO payments (id, order_id, amount_cents, status, "
            "failure_reason, attempt_count, risk_score, ip_country, device_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", PAYMENTS)

        conn.executemany(
            "INSERT INTO incidents (id, merchant_id, order_id, payment_id, kind, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", INCIDENTS)

    db.append_ledger("dataset_seeded", {"incidents": len(INCIDENTS)})
    print(f"Seeded {len(INCIDENTS)} incidents, {len(PAYMENTS)} payments.")


if __name__ == "__main__":
    seed()
