"""Tests for the per-day P&L attribution columns on eod_pnl.

Phase 2 transparency-inventory: closes the *P&L attribution* row in the
gate checklist by publishing the previously log-only NAV-reconciliation
breakdown as named columns.

The headline metric is `unattributed_residual_pct` = `unattributed_usd /
portfolio_nav × 100`. The inventory gate is ≤1%.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from executor.trade_logger import init_db, log_eod


@pytest.fixture
def conn():
    db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = init_db(db_path)
    yield c
    c.close()
    os.unlink(db_path)


# ── Schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_attribution_columns_present(self, conn):
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(eod_pnl)").fetchall()
        }
        expected = {
            "nav_change_usd", "position_pnl_usd", "interest_usd",
            "dividend_usd", "unattributed_usd", "unattributed_residual_pct",
        }
        missing = expected - cols
        assert not missing, f"missing attribution columns: {missing}"

    def test_init_db_idempotent(self):
        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            c1 = init_db(db_path)
            c1.close()
            c2 = init_db(db_path)
            c2.close()
            # After two init_db runs, the columns still exist exactly once.
            c3 = sqlite3.connect(db_path)
            cols = [
                row[1] for row in c3.execute("PRAGMA table_info(eod_pnl)").fetchall()
            ]
            c3.close()
            # Each name appears exactly once.
            assert cols.count("unattributed_residual_pct") == 1
            assert cols.count("unattributed_usd") == 1
        finally:
            os.unlink(db_path)


# ── log_eod plumbing ─────────────────────────────────────────────────────────


class TestLogEodAttribution:
    def test_attribution_fields_persisted(self, conn):
        log_eod(conn, {
            "date": "2026-05-06",
            "portfolio_nav": 100_000.0,
            "daily_return_pct": 0.5,
            "spy_return_pct": 0.3,
            "daily_alpha_pct": 0.2,
            "nav_change_usd": 500.0,
            "position_pnl_usd": 480.0,
            "interest_usd": 5.0,
            "dividend_usd": 12.0,
            "unattributed_usd": 15.0,
            "unattributed_residual_pct": 0.015,
        })
        row = conn.execute(
            "SELECT nav_change_usd, position_pnl_usd, interest_usd, "
            "dividend_usd, unattributed_usd, unattributed_residual_pct "
            "FROM eod_pnl WHERE date=?", ("2026-05-06",),
        ).fetchone()
        assert row == (500.0, 480.0, 5.0, 12.0, 15.0, 0.015)

    def test_legacy_caller_gets_null_attribution(self, conn):
        """A pre-PR caller that doesn't pass the new fields persists
        NULLs — the new columns are nullable per the additive-only
        schema policy. The row otherwise inserts cleanly."""
        log_eod(conn, {
            "date": "2026-05-06",
            "portfolio_nav": 100_000.0,
            "daily_return_pct": 0.5,
        })
        row = conn.execute(
            "SELECT nav_change_usd, position_pnl_usd, interest_usd, "
            "dividend_usd, unattributed_usd, unattributed_residual_pct "
            "FROM eod_pnl WHERE date=?", ("2026-05-06",),
        ).fetchone()
        assert row == (None, None, None, None, None, None)

    def test_csv_export_includes_attribution_columns(self, conn):
        """eod_reconcile re-exports `eod_pnl.csv` via SELECT *, so any
        new column flows automatically. Locks that contract — if a
        future schema change breaks SELECT * propagation, this fails."""
        import pandas as pd
        log_eod(conn, {
            "date": "2026-05-06",
            "portfolio_nav": 100_000.0,
            "unattributed_usd": 15.0,
            "unattributed_residual_pct": 0.015,
        })
        df = pd.read_sql("SELECT * FROM eod_pnl ORDER BY date", conn)
        assert "unattributed_residual_pct" in df.columns
        assert "unattributed_usd" in df.columns
        assert "nav_change_usd" in df.columns
        assert df.iloc[0]["unattributed_residual_pct"] == pytest.approx(0.015)

    def test_replace_preserves_attribution_overwrite(self, conn):
        """INSERT OR REPLACE on the same date overwrites all fields,
        not just the ones supplied — confirms the new attribution
        columns aren't accidentally treated as append-only."""
        log_eod(conn, {
            "date": "2026-05-06", "portfolio_nav": 100_000.0,
            "unattributed_residual_pct": 0.015,
        })
        log_eod(conn, {
            "date": "2026-05-06", "portfolio_nav": 100_500.0,
            "unattributed_residual_pct": 0.005,
        })
        row = conn.execute(
            "SELECT portfolio_nav, unattributed_residual_pct FROM eod_pnl "
            "WHERE date=?", ("2026-05-06",),
        ).fetchone()
        assert row == (100_500.0, 0.005)


# ── Residual % semantic locks ───────────────────────────────────────────────


class TestResidualPctSemantic:
    """The inventory gate is unattributed_residual_pct ≤ 1%. These
    tests document the values that the eod_reconcile producer is
    expected to write — they don't exercise the producer itself
    (which has its own logic tests in test_eod_reconcile_logic.py)
    but lock the meaning of the column."""

    def test_zero_residual_is_perfectly_attributed(self, conn):
        log_eod(conn, {"date": "2026-05-06", "portfolio_nav": 100_000.0,
                       "nav_change_usd": 500.0, "position_pnl_usd": 500.0,
                       "interest_usd": 0.0, "unattributed_usd": 0.0,
                       "unattributed_residual_pct": 0.0})
        row = conn.execute(
            "SELECT unattributed_residual_pct FROM eod_pnl WHERE date=?",
            ("2026-05-06",),
        ).fetchone()
        assert row[0] == 0.0

    def test_residual_pct_can_be_negative(self, conn):
        """Negative residual = position-pnl + interest exceeded the
        actual NAV change (e.g. an unaccounted fee). The column is
        signed; consumers should compare on absolute value."""
        log_eod(conn, {"date": "2026-05-06", "portfolio_nav": 100_000.0,
                       "nav_change_usd": 100.0, "position_pnl_usd": 200.0,
                       "interest_usd": 5.0, "unattributed_usd": -105.0,
                       "unattributed_residual_pct": -0.105})
        row = conn.execute(
            "SELECT unattributed_residual_pct FROM eod_pnl WHERE date=?",
            ("2026-05-06",),
        ).fetchone()
        assert row[0] == pytest.approx(-0.105)

    def test_above_1pct_breaches_inventory_gate(self, conn):
        """Sanity: a value > 1% is the alarm condition. The column
        captures it; alarm wiring is downstream (out of scope here)."""
        log_eod(conn, {"date": "2026-05-06", "portfolio_nav": 100_000.0,
                       "unattributed_usd": 1500.0,
                       "unattributed_residual_pct": 1.5})
        row = conn.execute(
            "SELECT unattributed_residual_pct FROM eod_pnl WHERE date=?",
            ("2026-05-06",),
        ).fetchone()
        assert row[0] > 1.0  # breach
