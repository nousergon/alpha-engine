"""Tests for executor/backfill_eod_pnl.py — ledger-synthesis recovery of a
missing eod_pnl row (config#1229)."""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from executor.backfill_eod_pnl import (
    backfill,
    day_cash_flow,
    replay_positions,
    synthesize_snapshot,
    _prior_eod_row,
)
from executor.trade_logger import init_db


def _seed_conn():
    conn = init_db(":memory:")
    return conn


def _trade(conn, *, tid, date, ticker, action, shares, fill_price=None, filled=None):
    conn.execute(
        "INSERT INTO trades (trade_id, date, ticker, action, shares, fill_price, "
        "filled_shares, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (tid, date, ticker, action, shares, fill_price, filled, f"{date}T12:00:00"),
    )
    conn.commit()


def _eod_row(conn, *, date, cash, accrued=0.0, positions=None):
    conn.execute(
        "INSERT OR REPLACE INTO eod_pnl (date, portfolio_nav, total_cash, "
        "accrued_interest, positions_snapshot, created_at) VALUES (?,?,?,?,?,?)",
        (date, None, cash, accrued, json.dumps(positions or {}), f"{date}T20:00:00"),
    )
    conn.commit()


# ── replay_positions ──────────────────────────────────────────────────────────


class TestReplayPositions:
    def test_nets_enter_exit_reduce(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-20", ticker="AAA", action="ENTER", shares=100)
        _trade(conn, tid="2", date="2026-06-22", ticker="AAA", action="REDUCE", shares=30)
        _trade(conn, tid="3", date="2026-06-20", ticker="BBB", action="ENTER", shares=50)
        _trade(conn, tid="4", date="2026-06-23", ticker="BBB", action="EXIT", shares=50)
        held = replay_positions(conn, "2026-06-24")
        assert held == {"AAA": 70}  # BBB fully exited → dropped

    def test_respects_as_of_date_cutoff(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-20", ticker="AAA", action="ENTER", shares=100)
        # A reduce AFTER the as-of date must not count.
        _trade(conn, tid="2", date="2026-06-25", ticker="AAA", action="REDUCE", shares=40)
        held = replay_positions(conn, "2026-06-24")
        assert held == {"AAA": 100}

    def test_uses_filled_shares_when_present(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-20", ticker="AAA", action="ENTER", shares=100, filled=80)
        assert replay_positions(conn, "2026-06-24") == {"AAA": 80}


# ── day_cash_flow ─────────────────────────────────────────────────────────────


class TestDayCashFlow:
    def test_no_trades_is_zero(self):
        conn = _seed_conn()
        assert day_cash_flow(conn, "2026-06-24") == 0.0

    def test_buy_out_sell_in(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-24", ticker="AAA", action="ENTER", shares=10, fill_price=100.0)
        _trade(conn, tid="2", date="2026-06-24", ticker="BBB", action="EXIT", shares=5, fill_price=200.0)
        # -10*100 (buy) + 5*200 (sell) = -1000 + 1000 = 0
        assert day_cash_flow(conn, "2026-06-24") == pytest.approx(0.0)

    def test_only_counts_the_target_date(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-23", ticker="AAA", action="ENTER", shares=10, fill_price=100.0)
        assert day_cash_flow(conn, "2026-06-24") == 0.0


# ── synthesize_snapshot ───────────────────────────────────────────────────────


class TestSynthesizeSnapshot:
    def test_nav_is_cash_plus_marked_positions(self):
        snap = synthesize_snapshot(
            run_date="2026-06-24",
            shares_by_ticker={"AAA": 100, "BBB": 50},
            closes_by_ticker={"AAA": 10.0, "BBB": 20.0},
            cash=5000.0,
            accrued_interest=12.0,
            prior_positions={"AAA": {"avg_cost": 9.0, "sector": "Tech"}},
            schema_version=1,
        )
        # NAV = 5000 + 100*10 + 50*20 = 7000
        assert snap["account"]["net_liquidation"] == pytest.approx(7000.0)
        assert snap["account"]["total_cash"] == 5000.0
        assert snap["account"]["accrued_interest"] == 12.0
        assert snap["synthesized"] is True
        assert snap["positions"]["AAA"]["avg_cost"] == 9.0       # carried from prior
        assert snap["positions"]["BBB"]["avg_cost"] == 20.0      # seeded to close (new)
        assert snap["positions"]["AAA"]["shares"] == 100

    def test_no_trade_day_is_exact_reprice_of_prior_book(self):
        # The common halt case: no trades, cash unchanged, prior book re-marked.
        snap = synthesize_snapshot(
            run_date="2026-06-24",
            shares_by_ticker={"AAA": 100},
            closes_by_ticker={"AAA": 11.0},
            cash=5000.0,
            accrued_interest=0.0,
            prior_positions={"AAA": {"avg_cost": 9.0}},
            schema_version=1,
        )
        assert snap["account"]["net_liquidation"] == pytest.approx(5000.0 + 100 * 11.0)


# ── _prior_eod_row ────────────────────────────────────────────────────────────


class TestPriorEodRow:
    def test_picks_latest_before_date_and_parses_snapshot(self):
        conn = _seed_conn()
        _eod_row(conn, date="2026-06-22", cash=100.0, positions={"AAA": {"avg_cost": 9.0}})
        _eod_row(conn, date="2026-06-23", cash=200.0, positions={"AAA": {"avg_cost": 9.5}})
        prior = _prior_eod_row(conn, "2026-06-24")
        assert prior["date"] == "2026-06-23" and prior["total_cash"] == 200.0
        assert prior["positions_snapshot"]["AAA"]["avg_cost"] == 9.5

    def test_none_when_no_prior(self):
        conn = _seed_conn()
        assert _prior_eod_row(conn, "2026-06-24") is None


# ── backfill orchestration (guards + dry-run) ─────────────────────────────────


class TestBackfillOrchestration:
    def _patch(self, stack, conn, closes):
        stack.enter_context(patch(
            "executor.backfill_eod_pnl.load_config",
            return_value={"db_path": ":memory:", "trades_bucket": "b", "aws_region": "us-east-1"},
        ))
        stack.enter_context(patch("executor.backfill_eod_pnl.init_db", return_value=conn))
        stack.enter_context(patch("executor.backfill_eod_pnl._read_closes_for_date", return_value=closes))
        stack.enter_context(patch("executor.snapshot_capturer.load_snapshot", return_value=None))

    def test_dry_run_no_trade_day_rolls_forward_exactly(self):
        conn = _seed_conn()
        _eod_row(conn, date="2026-06-23", cash=5000.0, positions={"AAA": {"avg_cost": 9.0}})
        _trade(conn, tid="1", date="2026-06-20", ticker="AAA", action="ENTER", shares=100)
        with ExitStack() as stack:
            self._patch(stack, conn, {"AAA": 11.0})
            result = backfill("2026-06-24", dry_run=True)
        assert result["dry_run"] is True
        assert result["cash_today"] == 5000.0                    # no trades on 06-24
        assert result["synthesized_nav"] == pytest.approx(5000.0 + 100 * 11.0)
        assert result["n_positions"] == 1

    def test_raises_when_no_prior_cash_baseline(self):
        conn = _seed_conn()
        _trade(conn, tid="1", date="2026-06-20", ticker="AAA", action="ENTER", shares=100)
        with ExitStack() as stack:
            self._patch(stack, conn, {"AAA": 11.0})
            with pytest.raises(RuntimeError, match="No prior eod_pnl row"):
                backfill("2026-06-24", dry_run=True)

    def test_raises_when_row_exists_without_force(self):
        conn = _seed_conn()
        _eod_row(conn, date="2026-06-23", cash=5000.0)
        _eod_row(conn, date="2026-06-24", cash=5100.0)  # the row already exists
        with ExitStack() as stack:
            self._patch(stack, conn, {})
            with pytest.raises(RuntimeError, match="already exists"):
                backfill("2026-06-24", dry_run=True)
