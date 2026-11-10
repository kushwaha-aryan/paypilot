"""Test fixtures.

The database path is read at import time from the environment, so it has to be
set before any `app` module is imported. That is why this lives in conftest.py
at the very top rather than in a fixture.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="paypilot-test-"))
os.environ["PAYPILOT_DB"] = str(_tmp / "test.db")
# Force deterministic planner and mock payments for tests, regardless of local .env.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["PAYPAL_MODE"] = "mock"

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture()
def fresh():
    """A clean, freshly seeded database for every test."""
    seed()
    yield
    db.init_db()
