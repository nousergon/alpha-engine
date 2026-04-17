"""
Load OHLCV price histories from the predictor's S3 caches.

Uses the slim cache (2y per-ticker parquets, refreshed weekly Sunday)
as the primary source. No new yfinance fetches required.

S3 layout:
    s3://alpha-engine-research/predictor/price_cache_slim/{TICKER}.parquet
    Columns: Open, High, Low, Close, Volume (capitalized)
    Index: DatetimeIndex (timezone-naive)

    s3://alpha-engine-research/predictor/daily_closes/{date}.parquet
    Columns: date, Open, High, Low, Close, Adj_Close, Volume, VWAP
    Index: ticker (str)
"""

from __future__ import annotations

# arcticdb MUST be imported before pandas on macOS to prime its bundled
# aws-c-common allocator before pyarrow (pulled in by pandas) loads its
# own copy. The two copies otherwise collide and arcticdb's S3Storage
# constructor segfaults with `aws_fatal_assert: allocator != ((void*)0)`
# on the first get_library() call. Linux runtimes (Lambda, EC2 Amazon
# Linux) are unaffected — dynamic linker resolves differently. arcticdb
# is a hard dep of the executor as of 2026-04-16 via requirements.txt;
# no fallback path, no optional import — feedback_no_silent_fails.
import arcticdb as _arcticdb  # noqa: F401  (kept for its side effect on import ordering)

import io
import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3
import pandas as pd

from executor.market_hours import is_trading_day

logger = logging.getLogger(__name__)


# Max staleness (in trading days) of the ATR feature before we hard-fail.
# 1 = yesterday's close is acceptable; anything older is treated as a
# pipeline-broken state and aborts the morning planner. Aligns with the
# predictor's own DailyData dependency expectation.
_ATR_MAX_STALENESS_TRADING_DAYS = 1


def _open_universe_library(signals_bucket: str):
    """Open the ArcticDB `universe` library for reads.

    Single connection helper used by every read path in the executor.
    Hard-fails on connection/library errors per feedback_no_silent_fails.
    """
    adb = _arcticdb  # already imported at module top for macOS allocator prime
    region = os.environ.get("AWS_REGION", "us-east-1")
    uri = (
        f"s3s://s3.{region}.amazonaws.com:{signals_bucket}"
        f"?path_prefix=arcticdb&aws_auth=true"
    )
    arctic = adb.Arctic(uri)
    return arctic.get_library("universe")


def _load_histories_from_arcticdb(
    tickers: list[str],
    signals_bucket: str,
) -> dict[str, list[dict]] | None:
    """Try to load price histories from ArcticDB universe library."""
    try:
        universe = _open_universe_library(signals_bucket)

        histories: dict[str, list[dict]] = {}
        for ticker in tickers:
            try:
                df = universe.read(ticker).data
                if df.empty:
                    continue
                records = []
                for dt, row in df.iterrows():
                    records.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "open": float(row["Open"]) if "Open" in row.index else 0.0,
                        "high": float(row["High"]) if "High" in row.index else 0.0,
                        "low": float(row["Low"]) if "Low" in row.index else 0.0,
                        "close": float(row["Close"]) if "Close" in row.index else 0.0,
                    })
                histories[ticker] = records
            except Exception:
                pass

        if histories:
            logger.info("[data_source=arcticdb] Price histories loaded for %d/%d tickers", len(histories), len(tickers))
            return histories
    except ImportError:
        logger.debug("arcticdb not installed — using S3 slim cache")
    except Exception as e:
        logger.debug("[data_source=arcticdb] ArcticDB load failed: %s", e)
    return None


def load_price_histories(
    tickers: list[str],
    signals_bucket: str,
) -> dict[str, list[dict]]:
    """
    Load OHLCV histories for a list of tickers.

    Priority: ArcticDB universe → S3 slim cache parquets.

    Returns:
        {ticker: [{date, open, high, low, close}, ...]} sorted ascending by date.
        Tickers without cached data are omitted.
    """
    # Try ArcticDB first
    arctic_result = _load_histories_from_arcticdb(tickers, signals_bucket)
    if arctic_result is not None:
        return arctic_result

    # Legacy: S3 slim cache
    s3 = boto3.client("s3")
    histories: dict[str, list[dict]] = {}

    for ticker in tickers:
        key = f"predictor/price_cache_slim/{ticker}.parquet"
        try:
            obj = s3.get_object(Bucket=signals_bucket, Key=key)
            df = pd.read_parquet(io.BytesIO(obj["Body"].read()))
        except Exception as e:
            logger.debug(f"No slim cache for {ticker}: {e}")
            continue

        if df.empty:
            continue

        # Normalize column names to lowercase for exit_manager compatibility
        df.columns = [c.lower() for c in df.columns]

        # Index is DatetimeIndex — convert to date strings
        records = []
        for dt, row in df.iterrows():
            records.append({
                "date": dt.strftime("%Y-%m-%d"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })

        histories[ticker] = records
        logger.debug(f"Loaded {len(records)} bars for {ticker} from slim cache")

    logger.info("[data_source=legacy] Price histories loaded for %d/%d tickers from S3 slim cache", len(histories), len(tickers))
    return histories


def load_atr_14_pct(
    tickers: list[str],
    signals_bucket: str,
    max_staleness_trading_days: int = _ATR_MAX_STALENESS_TRADING_DAYS,
    reference_date: date | None = None,
) -> dict[str, float]:
    """
    Read the most recent `atr_14_pct` value per ticker from the ArcticDB
    universe library. Single source of truth for ATR across the executor —
    pullback trigger scaling, position sizing, and trailing stops all
    consume from this map to eliminate intra-executor ATR-definition drift
    (previously each call site computed its own ATR via _compute_atr from
    raw OHLC, which could subtly diverge from the predictor's feature
    store definition of atr_14_pct).

    Values are stored in ArcticDB as decimals (e.g. 0.0238 = 2.38%),
    consistent with how the pullback trigger config's pullback_pct is
    interpreted, so no unit conversion is needed downstream.

    Hard-fails per feedback_hard_fail_until_stable:
      - arcticdb import failure (missing dep) → ImportError raised
      - ArcticDB connection/library access failure → original exception
        propagated (no silent fallback)
      - Any requested ticker missing `atr_14_pct` column → RuntimeError
      - Any requested ticker whose most-recent row is older than
        `max_staleness_trading_days` → RuntimeError
      - Any ticker with a non-finite or non-positive atr_14_pct → RuntimeError

    Args:
        tickers: Tickers to look up. Must all be present in universe library.
        signals_bucket: S3 bucket hosting the ArcticDB store (same as
                        research/predictor).
        max_staleness_trading_days: Reject data older than this many trading
                                    days from reference_date.
        reference_date: Date to measure staleness against. Defaults to today
                        (UTC). Pass an explicit date in tests.

    Returns:
        {ticker: atr_14_pct} for every requested ticker. Raises if any
        fails validation.
    """
    if not tickers:
        return {}

    universe = _open_universe_library(signals_bucket)

    ref = reference_date or datetime.now(timezone.utc).date()
    staleness_cutoff = _n_trading_days_back(ref, max_staleness_trading_days)

    atr_map: dict[str, float] = {}
    missing_feature: list[str] = []
    missing_symbol: list[str] = []
    stale: list[tuple[str, str]] = []
    invalid: list[tuple[str, float]] = []

    for ticker in tickers:
        try:
            df = universe.read(ticker).data
        except Exception as e:
            missing_symbol.append(f"{ticker} ({e.__class__.__name__})")
            continue

        if "atr_14_pct" not in df.columns:
            missing_feature.append(ticker)
            continue

        if df.empty:
            missing_symbol.append(f"{ticker} (empty frame)")
            continue

        last_dt = df.index[-1]
        last_date = last_dt.date() if hasattr(last_dt, "date") else pd.Timestamp(last_dt).date()
        if last_date < staleness_cutoff:
            stale.append((ticker, str(last_date)))
            continue

        val = float(df["atr_14_pct"].iloc[-1])
        if not (val == val and val > 0):  # NaN-safe positivity check
            invalid.append((ticker, val))
            continue

        atr_map[ticker] = val

    problems = []
    if missing_symbol:
        problems.append(f"missing_symbol={missing_symbol}")
    if missing_feature:
        problems.append(f"missing_feature={missing_feature}")
    if stale:
        problems.append(
            f"stale (older than {max_staleness_trading_days} trading day"
            f"{'s' if max_staleness_trading_days != 1 else ''} before "
            f"{ref}, cutoff={staleness_cutoff})={stale}"
        )
    if invalid:
        problems.append(f"non-finite-or-non-positive={invalid}")

    if problems:
        raise RuntimeError(
            "load_atr_14_pct failed validation — executor morning planner cannot "
            "proceed without a trustworthy ATR for every signal ticker. "
            f"Requested {len(tickers)} tickers, resolved {len(atr_map)}. "
            "Problems: " + "; ".join(problems)
        )

    logger.info(
        "[data_source=arcticdb] Loaded atr_14_pct for %d/%d tickers (cutoff=%s)",
        len(atr_map), len(tickers), staleness_cutoff,
    )
    return atr_map


def _n_trading_days_back(ref: date, n: int) -> date:
    """Walk back `n` trading days from `ref` (inclusive of today if it's
    a trading day). Weekend/holiday skipping uses the same calendar the
    rest of the executor consults."""
    current = ref
    remaining = n
    # Start on a trading day
    while not is_trading_day(current):
        current -= timedelta(days=1)
    while remaining > 0:
        current -= timedelta(days=1)
        while not is_trading_day(current):
            current -= timedelta(days=1)
        remaining -= 1
    return current


def load_daily_vwap(
    tickers: list[str],
    signals_bucket: str,
    run_date: str | None = None,
    max_lookback: int = 5,
) -> dict[str, float]:
    """Load prior-day VWAP per ticker from the ArcticDB universe library.

    For each requested ticker, walks back from run_date (skipping
    weekends/holidays) and returns the most recent VWAP value within
    `max_lookback` trading days. Hard-fails if the universe library is
    unreachable or has no VWAP column. Tickers whose entire lookback
    window has no VWAP are raised as a single failure (no silent empty
    dict) — VWAP is a daemon entry-trigger input and must be trusted.
    """
    if not tickers:
        return {}

    universe = _open_universe_library(signals_bucket)
    start = date.fromisoformat(run_date) if run_date else date.today()

    # Build the list of candidate trading dates once — all tickers scan
    # the same window. Normalize to date for filtering.
    candidates: list[date] = []
    for days_back in range(max_lookback + 1):
        candidate = start - timedelta(days=days_back)
        if candidate.weekday() > 4:
            continue
        if not is_trading_day(candidate):
            continue
        candidates.append(candidate)
    if not candidates:
        raise RuntimeError(
            f"No trading-day candidates within {max_lookback} days of {start}"
        )

    # Contract:
    #   HARD FAIL on library/read errors — infrastructure problem.
    #   PARTIAL COVERAGE (INFO log) when a ticker's frame has no VWAP column
    #       or no valid VWAP in the lookback window. VWAP was added to the
    #       universe schema 2026-04-17; historical ticker frames + yfinance-
    #       sourced rows legitimately lack it. The daemon's VWAP-discount
    #       trigger explicitly skips tickers with no VWAP (entry_triggers.py:
    #       `if vwap and vwap > 0`), so a documented data gap is tolerable
    #       while other triggers (pullback, support, time expiry) carry load.
    read_errors: list[str] = []
    no_vwap_column: list[str] = []
    no_valid_vwap_in_window: list[str] = []
    vwap_map: dict[str, float] = {}

    for ticker in tickers:
        try:
            df = universe.read(ticker).data
        except Exception as e:
            read_errors.append(f"{ticker} ({e.__class__.__name__})")
            continue
        if df.empty or "VWAP" not in df.columns:
            no_vwap_column.append(ticker)
            continue
        # Find the most recent row whose index matches one of the candidate
        # trading days (normalized). First hit wins.
        idx = df.index.normalize() if hasattr(df.index, "normalize") else df.index
        for cand in candidates:
            match = df[idx == pd.Timestamp(cand)]
            if match.empty:
                continue
            v = match["VWAP"].iloc[-1]
            if pd.notna(v) and v > 0:
                vwap_map[ticker] = float(v)
                break
        if ticker not in vwap_map:
            no_valid_vwap_in_window.append(ticker)

    if read_errors:
        raise RuntimeError(
            f"load_daily_vwap ArcticDB read failed for {len(read_errors)} "
            f"ticker(s): {read_errors}. Daemon cannot plan triggers without "
            "a trusted universe library."
        )

    logger.info(
        "[data_source=arcticdb] VWAP resolved for %d/%d tickers "
        "(window ≤ %s, no_column=%d, no_valid=%d)",
        len(vwap_map), len(tickers), start,
        len(no_vwap_column), len(no_valid_vwap_in_window),
    )
    if no_vwap_column:
        logger.info(
            "VWAP column absent for %d ticker(s) — daemon skips VWAP "
            "trigger for these: %s",
            len(no_vwap_column), sorted(no_vwap_column),
        )
    if no_valid_vwap_in_window:
        logger.info(
            "No valid VWAP in %d-day window for %d ticker(s): %s",
            max_lookback, len(no_valid_vwap_in_window),
            sorted(no_valid_vwap_in_window),
        )
    return vwap_map
