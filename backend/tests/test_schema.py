"""Schema and seed-data tests.

Step 1 of the PayPal integration only changes storage: internal ids stay the
same, PayPal ids live in their own columns, and an existing database is upgraded
in place instead of being thrown away. These tests pin all three properties.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app import db


def _columns(table: str) -> set[str]:
    return {row["name"] for row in db.query(f"PRAGMA table_info({table})")}


def test_external_id_columns_exist(fresh):
    assert "paypal_order_id" in _columns("orders")
    assert {"paypal_capture_id", "paypal_order_id"} <= _columns("payments")


def test_refunds_table_has_expected_shape(fresh):
    cols = {row["name"]: row for row in db.query("PRAGMA table_info(refunds)")}
    assert set(cols) == {
        "id",
        "payment_id",
        "capture_id",
        "paypal_refund_id",
        "amount_cents",
        "currency",
        "status",
        "idempotency_key",
        "created_at",
    }
    assert cols["payment_id"]["notnull"] == 1

    fks = db.query("PRAGMA foreign_key_list(refunds)")
    assert any(
        fk["table"] == "payments" and fk["from"] == "payment_id" for fk in fks
    ), "refunds.payment_id must keep its foreign key to payments"


def test_seed_keeps_internal_ids_and_relationships(fresh):
    payments = db.query("SELECT * FROM payments ORDER BY id")
    orders = db.query("SELECT * FROM orders ORDER BY id")
    incidents = db.query("SELECT * FROM incidents ORDER BY id")

    assert [p["id"] for p in payments] == [f"P-100{i}" for i in range(1, 7)]
    assert [o["id"] for o in orders] == [f"O-100{i}" for i in range(1, 7)]
    assert [i["id"] for i in incidents] == [f"INC-00{i}" for i in range(1, 7)]

    order_ids = {o["id"] for o in orders}
    payment_ids = {p["id"] for p in payments}
    for payment in payments:
        assert payment["order_id"] in order_ids
    for incident in incidents:
        assert incident["order_id"] in order_ids
        assert incident["payment_id"] in payment_ids

    # Seeded demo rows are internal-only; real PayPal ids arrive later.
    for payment in payments:
        assert payment["paypal_capture_id"] is None
        assert payment["paypal_order_id"] is None
    assert all(order["paypal_order_id"] is None for order in orders)
    assert db.query_one("SELECT COUNT(*) AS n FROM refunds")["n"] == 0


def test_seed_reset_clears_refunds_before_payments(fresh):
    db.execute(
        "INSERT INTO refunds (id, payment_id, capture_id, paypal_refund_id, "
        "amount_cents, currency, status, idempotency_key, created_at) "
        "VALUES ('R-1', 'P-1004', 'CAP-1', 'REF-1', 2500, 'USD', 'completed', "
        "'seed-test', ?)",
        (db.utcnow(),),
    )

    from app.seed import seed

    seed()  # would hit the payments FK if refunds were not cleared first
    assert db.query_one("SELECT COUNT(*) AS n FROM refunds")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM payments")["n"] == 6


def test_existing_database_is_migrated_in_place(tmp_path, monkeypatch):
    """An old SQLite file keeps its rows and gains the new columns."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE orders (
            id            TEXT PRIMARY KEY,
            merchant_id   TEXT NOT NULL,
            customer_id   TEXT NOT NULL,
            amount_cents  INTEGER NOT NULL,
            currency      TEXT NOT NULL DEFAULT 'USD',
            created_at    TEXT NOT NULL,
            status        TEXT NOT NULL
        );
        CREATE TABLE payments (
            id             TEXT PRIMARY KEY,
            order_id       TEXT NOT NULL,
            amount_cents   INTEGER NOT NULL,
            currency       TEXT NOT NULL DEFAULT 'USD',
            status         TEXT NOT NULL,
            failure_reason TEXT,
            attempt_count  INTEGER NOT NULL DEFAULT 1,
            risk_score     REAL NOT NULL DEFAULT 0.0,
            ip_country     TEXT,
            device_id      TEXT,
            created_at     TEXT NOT NULL
        );
        INSERT INTO orders
            (id, merchant_id, customer_id, amount_cents, currency, created_at, status)
            VALUES ('O-legacy', 'M-1', 'C-1', 1000, 'USD', '2026-01-01', 'created');
        INSERT INTO payments
            (id, order_id, amount_cents, currency, status, failure_reason,
             attempt_count, risk_score, ip_country, device_id, created_at)
            VALUES ('P-legacy', 'O-legacy', 1000, 'USD', 'succeeded', NULL,
                    1, 0.0, 'US', 'DEV-A', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "settings", SimpleNamespace(db_path=str(legacy)))

    added = db.init_db()
    assert "orders.paypal_order_id" in added
    assert "payments.paypal_capture_id" in added
    assert "payments.paypal_order_id" in added

    order = db.query_one("SELECT * FROM orders WHERE id = 'O-legacy'")
    payment = db.query_one("SELECT * FROM payments WHERE id = 'P-legacy'")
    assert order is not None and order["amount_cents"] == 1000
    assert payment is not None and payment["order_id"] == "O-legacy"
    assert order["paypal_order_id"] is None
    assert payment["paypal_capture_id"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM refunds")["n"] == 0

    # Running init again is a no-op, not another migration.
    assert db.init_db() == []


def test_env_example_contains_no_committed_credentials():
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    for key in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
        value = values[key].lower()
        assert value == "" or value.startswith(("your", "replace", "<")), (
            f"{key} must be a placeholder in the committed .env.example"
        )
