"""Tests for executor.price_cache.load_atr_14_pct.

The hard-fail contract is the thing this test suite most cares about:
the morning planner must not silently ship a degenerate 0.0 ATR for any
signal ticker, because the pullback-trigger and trailing-stop
calculations downstream assume a positive, fresh value. If the feature
store is missing a column or stale, the planner should abort loudly.
"""

from __future__ import annotations

import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Inject a fake arcticdb module so local test runs work without the real
# arcticdb install (EC2 deploy pulls it via requirements.txt). Tests that
# call load_atr_14_pct patch sys.modules["arcticdb"].Arctic to return a
# mock Arctic instance.
if "arcticdb" not in sys.modules:
    _fake_arcticdb = MagicMock()
    sys.modules["arcticdb"] = _fake_arcticdb

from executor import price_cache  # noqa: E402
from executor.price_cache import _n_trading_days_back, load_atr_14_pct  # noqa: E402


def _mock_arctic_lib(ticker_rows: dict[str, pd.DataFrame]):
    """Build a mock arcticdb.Arctic → universe library that returns
    the given DataFrame for each ticker read."""
    lib = MagicMock()

    def _read(ticker):
        if ticker not in ticker_rows:
            raise KeyError(f"no such symbol: {ticker}")
        result = MagicMock()
        result.data = ticker_rows[ticker]
        return result

    lib.read.side_effect = _read

    arctic = MagicMock()
    arctic.get_library.return_value = lib
    return arctic


def _df(atr_values: list[float], last_date: date) -> pd.DataFrame:
    """Synthesize a universe DataFrame with an atr_14_pct column ending
    on `last_date`. Index is a DatetimeIndex of consecutive business
    days working backward from last_date."""
    n = len(atr_values)
    index = pd.bdate_range(end=pd.Timestamp(last_date), periods=n)
    return pd.DataFrame({"atr_14_pct": atr_values}, index=index)


class TestLoadAtr14Pct:
    def test_returns_map_of_latest_values(self):
        ref = date(2026, 4, 16)  # Thursday
        rows = {
            "AAPL": _df([0.020, 0.022, 0.024], last_date=ref),
            "KO": _df([0.008, 0.009, 0.010], last_date=ref),
        }
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                result = load_atr_14_pct(
                    tickers=["AAPL", "KO"],
                    signals_bucket="test-bucket",
                    reference_date=ref,
                )
        assert result == {"AAPL": 0.024, "KO": 0.010}

    def test_empty_ticker_list_returns_empty_map_no_arctic_call(self):
        """No tickers means no ArcticDB connection — cheap short-circuit."""
        with patch.object(price_cache._arcticdb, "Arctic") as mock_arctic:
            result = load_atr_14_pct(tickers=[], signals_bucket="test-bucket")
            assert result == {}
            mock_arctic.assert_not_called()

    def test_hard_fails_on_missing_ticker(self):
        """Contract: every requested ticker must resolve. If one is missing,
        raise — we must not silently ship a zero ATR into the order book."""
        ref = date(2026, 4, 16)
        rows = {"AAPL": _df([0.024], last_date=ref)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                with pytest.raises(RuntimeError, match="missing_symbol.*UNKNOWN"):
                    load_atr_14_pct(
                        tickers=["AAPL", "UNKNOWN"],
                        signals_bucket="test-bucket",
                        reference_date=ref,
                    )

    def test_hard_fails_on_missing_atr_column(self):
        """If a ticker has a frame but no atr_14_pct column (e.g. partial
        backfill or a new symbol not yet feature-computed), fail loud."""
        ref = date(2026, 4, 16)
        no_atr = pd.DataFrame(
            {"Close": [100.0]}, index=pd.bdate_range(end=pd.Timestamp(ref), periods=1),
        )
        rows = {"BROKEN": no_atr}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                with pytest.raises(RuntimeError, match="missing_feature.*BROKEN"):
                    load_atr_14_pct(
                        tickers=["BROKEN"],
                        signals_bucket="test-bucket",
                        reference_date=ref,
                    )

    def test_hard_fails_on_stale_data(self):
        """Data older than max_staleness_trading_days must abort. Stale ATR
        feeding the pullback trigger would recalibrate to conditions that
        no longer apply."""
        ref = date(2026, 4, 16)
        stale_date = date(2026, 4, 10)  # Friday, >1 trading day back from Wed 4/16
        rows = {"STALE": _df([0.02], last_date=stale_date)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                with pytest.raises(RuntimeError, match="stale"):
                    load_atr_14_pct(
                        tickers=["STALE"],
                        signals_bucket="test-bucket",
                        reference_date=ref,
                        max_staleness_trading_days=1,
                    )

    def test_hard_fails_on_non_positive_atr(self):
        """ATR should never be zero or negative for any real-world ticker —
        if we see one, the feature-compute pipeline upstream is broken
        and we shouldn't ship bogus signals into the trading path."""
        ref = date(2026, 4, 16)
        rows = {"ZEROATR": _df([0.0], last_date=ref)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                with pytest.raises(RuntimeError, match="non-finite-or-non-positive"):
                    load_atr_14_pct(
                        tickers=["ZEROATR"],
                        signals_bucket="test-bucket",
                        reference_date=ref,
                    )

    def test_hard_fails_on_nan_atr(self):
        ref = date(2026, 4, 16)
        rows = {"NAN": _df([float("nan")], last_date=ref)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(price_cache._arcticdb, "Arctic", return_value=_mock_arctic_lib(rows)):
                with pytest.raises(RuntimeError, match="non-finite-or-non-positive"):
                    load_atr_14_pct(
                        tickers=["NAN"],
                        signals_bucket="test-bucket",
                        reference_date=ref,
                    )


class TestNTradingDaysBack:
    def test_one_trading_day_back_from_weekday(self):
        """If ref is a weekday and is_trading_day returns True for every
        weekday, n=1 means yesterday (weekday)."""
        ref = date(2026, 4, 16)  # Thursday
        with patch.object(price_cache, "is_trading_day", return_value=True):
            assert _n_trading_days_back(ref, 1) == date(2026, 4, 15)

    def test_zero_trading_days_back_returns_ref_if_trading_day(self):
        ref = date(2026, 4, 16)
        with patch.object(price_cache, "is_trading_day", return_value=True):
            assert _n_trading_days_back(ref, 0) == ref

    def test_skips_weekends(self):
        """Monday - 1 trading day should land on the previous Friday, not Sunday."""
        monday = date(2026, 4, 13)  # Mon
        # Mock is_trading_day to reflect reality: Sat/Sun are not trading days
        def _is_td(d):
            return d.weekday() < 5
        with patch.object(price_cache, "is_trading_day", side_effect=_is_td):
            assert _n_trading_days_back(monday, 1) == date(2026, 4, 10)  # Friday


def _df_vwap(values: list[float], last_date: date, col: str = "VWAP") -> pd.DataFrame:
    """Synthesize a universe-style frame with a VWAP column."""
    n = len(values)
    index = pd.bdate_range(end=pd.Timestamp(last_date), periods=n)
    return pd.DataFrame({col: values}, index=index)


class TestLoadDailyVwap:
    """Phase 2: load_daily_vwap reads from ArcticDB — no parquet fallback."""

    def test_empty_tickers_short_circuits(self):
        with patch.object(price_cache._arcticdb, "Arctic") as mock_arctic:
            result = price_cache.load_daily_vwap([], "test-bucket", run_date="2026-04-17")
            assert result == {}
            mock_arctic.assert_not_called()

    def test_returns_latest_vwap_for_each_ticker(self):
        ref = date(2026, 4, 17)  # Friday
        rows = {
            "AAPL": _df_vwap([172.0, 173.5, 175.0], last_date=ref),
            "MSFT": _df_vwap([410.0, 412.5, 415.0], last_date=ref),
        }
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(
                price_cache._arcticdb, "Arctic",
                return_value=_mock_arctic_lib(rows),
            ):
                result = price_cache.load_daily_vwap(
                    ["AAPL", "MSFT"], "test-bucket", run_date="2026-04-17",
                )
        assert result == {"AAPL": 175.0, "MSFT": 415.0}

    def test_hard_fails_on_arcticdb_read_error(self):
        """Library read error (symbol missing, connection down, etc.) → raise.

        The symbol-missing case is an infrastructure error, not a data gap —
        the universe should contain every ENTER-signal ticker.
        """
        ref = date(2026, 4, 17)
        rows = {"AAPL": _df_vwap([172.0], last_date=ref)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(
                price_cache._arcticdb, "Arctic",
                return_value=_mock_arctic_lib(rows),
            ):
                with pytest.raises(RuntimeError, match="ArcticDB read failed"):
                    price_cache.load_daily_vwap(
                        ["AAPL", "NOPE"], "test-bucket", run_date="2026-04-17",
                    )

    def test_tolerates_missing_vwap_column(self):
        """Ticker exists in universe but has no VWAP column → omit from map,
        don't raise. VWAP was added to the schema 2026-04-17; historical
        frames legitimately lack it. The daemon handles a missing VWAP entry
        by skipping that trigger, so partial coverage is a documented gap."""
        ref = date(2026, 4, 17)
        rows = {
            "AAPL": _df_vwap([172.0], last_date=ref),  # has VWAP
            "OLD":  _df_vwap([99.0], last_date=ref, col="Close"),  # no VWAP col
        }
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(
                price_cache._arcticdb, "Arctic",
                return_value=_mock_arctic_lib(rows),
            ):
                result = price_cache.load_daily_vwap(
                    ["AAPL", "OLD"], "test-bucket", run_date="2026-04-17",
                )
        assert result == {"AAPL": 172.0}
        assert "OLD" not in result

    def test_walks_back_through_lookback_window(self):
        """If run_date has no row, pick the next most recent trading day in window."""
        # Only has data 2 days back
        ref = date(2026, 4, 15)  # Wednesday
        rows = {"AAPL": _df_vwap([170.0], last_date=ref)}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(
                price_cache._arcticdb, "Arctic",
                return_value=_mock_arctic_lib(rows),
            ):
                result = price_cache.load_daily_vwap(
                    ["AAPL"], "test-bucket", run_date="2026-04-17",  # Friday
                )
        assert result == {"AAPL": 170.0}

    def test_tolerates_empty_window(self):
        """No VWAP in any candidate day → omit from map with INFO log, not raise."""
        # Data exists but all before the window
        rows = {"AAPL": _df_vwap([170.0], last_date=date(2026, 1, 5))}
        with patch.object(price_cache, "is_trading_day", return_value=True):
            with patch.object(
                price_cache._arcticdb, "Arctic",
                return_value=_mock_arctic_lib(rows),
            ):
                result = price_cache.load_daily_vwap(
                    ["AAPL"], "test-bucket", run_date="2026-04-17",
                )
        assert result == {}
