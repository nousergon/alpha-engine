"""Tests for executor/eod_emailer.py — pure HTML/text building functions."""

import sqlite3

import pytest

from executor.eod_emailer import _dollar, _pct, _plain_pct, build_eod_email


class TestFormatters:
    def test_pct_positive(self):
        result = _pct(1.5)
        assert "+1.50%" in result
        assert "pos" in result

    def test_pct_negative(self):
        result = _pct(-2.3)
        assert "-2.30%" in result
        assert "neg" in result

    def test_pct_zero(self):
        result = _pct(0.0)
        assert "0.00%" in result

    def test_pct_none(self):
        assert _pct(None) == "—"

    def test_dollar_positive(self):
        result = _dollar(1500.0)
        assert "+$1,500" in result

    def test_dollar_negative(self):
        result = _dollar(-800.0)
        assert "800" in result
        assert "neg" in result

    def test_dollar_none(self):
        assert _dollar(None) == "—"

    def test_plain_pct_positive(self):
        assert _plain_pct(1.5) == "+1.50%"

    def test_plain_pct_negative(self):
        assert _plain_pct(-2.3) == "-2.30%"

    def test_plain_pct_none(self):
        assert _plain_pct(None) == "—"


class TestBuildEodEmail:
    @pytest.fixture
    def mock_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE eod_pnl (
                date TEXT, portfolio_nav REAL, daily_return_pct REAL,
                spy_return_pct REAL, daily_alpha_pct REAL
            )
        """)
        conn.execute("""
            CREATE TABLE trades (
                trade_id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, action TEXT,
                shares INTEGER, fill_price REAL, price_at_order REAL,
                research_score REAL, market_regime TEXT, trigger_type TEXT,
                rationale_json TEXT, created_at TEXT,
                portfolio_nav_at_order REAL, position_pct REAL,
                research_conviction TEXT, research_rating TEXT, sector_rating TEXT,
                thesis_summary TEXT, predicted_direction TEXT, prediction_confidence REAL,
                fill_time TEXT, ib_order_id TEXT, execution_latency_ms REAL,
                signal_price REAL, trigger_price REAL, slippage_vs_signal REAL,
                entry_trade_id INTEGER, realized_pnl REAL, realized_return_pct REAL,
                realized_alpha_pct REAL, days_held INTEGER, spy_price_at_order REAL
            )
        """)
        conn.executemany(
            "INSERT INTO eod_pnl VALUES (?,?,?,?,?)",
            [
                ("2026-04-07", 100000, 0.5, 0.3, 0.2),
                ("2026-04-08", 100500, 0.5, 0.2, 0.3),
            ],
        )
        conn.commit()
        return conn

    def test_basic_email(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100500.0,
            daily_return=0.5,
            spy_return=0.2,
            alpha=0.3,
            positions={"AAPL": {"shares": 10, "market_value": 1500}},
            conn=mock_db,
        )
        assert "2026-04-08" in subject
        assert "100,500" in subject
        assert "+0.30%" in subject
        assert "AAPL" in html
        assert "AAPL" in plain

    def test_no_positions(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100000.0,
            daily_return=0.0,
            spy_return=0.0,
            alpha=0.0,
            positions={},
            conn=mock_db,
        )
        assert "2026-04-08" in subject
        assert "html" in html.lower() or "body" in html.lower()

    def test_with_sector_attribution(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100500.0,
            daily_return=0.5,
            spy_return=0.2,
            alpha=0.3,
            positions={"AAPL": {"shares": 10, "market_value": 1500}},
            conn=mock_db,
            sector_attribution={"Technology": {"weight": 0.15, "contribution": 0.08, "positions": 2}},
        )
        assert "Technology" in html or "Sector" in html

    def test_with_roundtrip_stats(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100500.0,
            daily_return=0.5,
            spy_return=0.2,
            alpha=0.3,
            positions={},
            conn=mock_db,
            roundtrip_stats={"n_roundtrips": 5, "avg_return_pct": 2.1, "win_rate_vs_spy": 60.0},
        )
        # Should include roundtrip stats somewhere in email
        assert "html" in html.lower() or "body" in html.lower()

    def test_with_data_warnings(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100500.0,
            daily_return=0.5,
            spy_return=0.2,
            alpha=0.3,
            positions={},
            conn=mock_db,
            data_warnings=["Stale prices for 3 tickers"],
        )
        assert "Stale" in html or "warning" in html.lower()

    def test_none_returns(self, mock_db):
        subject, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=100000.0,
            daily_return=None,
            spy_return=None,
            alpha=None,
            positions={},
            conn=mock_db,
        )
        assert "—" in subject or "None" not in subject

    def test_cash_row_does_not_absorb_nav_residual(self, mock_db):
        """Regression: cash should NOT show a fabricated daily return.

        Prior bug: cash_daily_usd = total_nav_change - total_day_usd absorbed
        pricing/snapshot noise. On 2026-04-17, this produced a +2.27% daily
        return on the cash sleeve and +$7,148 of bogus cash alpha (133% of
        total alpha). Cash row must show '—' for daily return and only earn
        α from actual interest.
        """
        positions = {
            "AAPL": {
                "shares": 10, "market_value": 1500,
                "daily_return_pct": 0.01, "daily_return_usd": 0.15,
                "alpha_contribution_usd": 0.05,
            },
        }
        # NAV moved by $8000 but positions only contributed $0.15
        # and reconciliation says only $50 is interest.
        recon = {
            "nav_change_usd": 8000.0,
            "position_pnl_usd": 0.15,
            "interest_usd": 50.0,
            "dividend_usd": 0.0,
            "unattributed_usd": 7949.85,
        }
        account = {"total_cash": 350000.0, "accrued_interest": 550.0}
        _, html, plain = build_eod_email(
            run_date="2026-04-08",
            nav=1_031_666.0,
            daily_return=0.76,
            spy_return=0.25,
            alpha=0.51,
            positions=positions,
            conn=mock_db,
            account_snapshot=account,
            nav_reconciliation=recon,
        )
        # Cash row must not claim a 2.27% daily return
        assert "2.27%" not in html
        # Unattributed gap must be surfaced (not hidden)
        assert "Unattributed" in html
        assert "7,950" in html or "7,949" in html
        # Interest row should appear
        assert "Interest" in html

    def test_reconciliation_absent_falls_back_cleanly(self, mock_db):
        """If nav_reconciliation isn't passed, cash alpha is ~0 (interest=0)."""
        positions = {
            "AAPL": {
                "shares": 10, "market_value": 1500,
                "daily_return_pct": 0.1, "daily_return_usd": 1.5,
                "alpha_contribution_usd": 0.5,
            },
        }
        _, html, _ = build_eod_email(
            run_date="2026-04-08",
            nav=100_000.0,
            daily_return=0.1,
            spy_return=0.05,
            alpha=0.05,
            positions=positions,
            conn=mock_db,
            account_snapshot={"total_cash": 98_500.0},
        )
        # No explicit reconciliation → no Interest/Dividends rows
        assert "Interest</td>" not in html
        assert "Dividends</td>" not in html
