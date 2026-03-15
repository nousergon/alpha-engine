"""
Technical scoring engine for the Executor — deterministic, no LLM.

Computes a 0–100 technical attractiveness score from price-derived indicators:
  RSI(14)            25% weight  [regime-aware thresholds]
  MACD signal cross  20% weight
  Price vs 50-day MA 20% weight
  Price vs 200-day MA 20% weight
  20-day momentum    15% weight  [percentile-ranked across population]

Copied from alpha-engine-research/scoring/technical.py to keep the Executor
self-contained (no cross-repo imports).  The indicator computation wrapper
(compute_indicators_from_ohlcv) uses the same formulas as
alpha-engine-predictor/data/feature_engineer.py.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ── Per-signal scoring ────────────────────────────────────────────────────────
# (Copied verbatim from alpha-engine-research/scoring/technical.py)

def _score_rsi(rsi: float, market_regime: str = "neutral") -> float:
    """
    Score RSI (0–100) with regime-aware overbought/oversold thresholds.

    Bull regime (VIX<15, uptrend): raise overbought threshold to 80.
    Bear/caution regime: raise oversold threshold to 40 (oversold can signal
      further decline, not necessarily a buy).
    Neutral: standard 30/70 thresholds.
    """
    if market_regime == "bull":
        overbought = 80
        oversold = 30
        max_oversold_score = 100.0
    elif market_regime in ("bear", "caution"):
        overbought = 70
        oversold = 40
        max_oversold_score = 65.0
    else:  # neutral
        overbought = 70
        oversold = 30
        max_oversold_score = 100.0

    if rsi >= overbought:
        return 0.0
    if rsi <= oversold:
        return max_oversold_score
    return max_oversold_score * (overbought - rsi) / (overbought - oversold)


def _score_macd(macd_cross: float, macd_above_zero: bool) -> float:
    """
    Score MACD signal cross.
    Bullish cross above zero = 100
    Bullish cross below zero = 70
    No cross, above zero = 60
    No cross, below zero = 40
    Bearish cross above zero = 30
    Bearish cross below zero = 0
    """
    if macd_cross == 1.0:  # bullish cross
        return 100.0 if macd_above_zero else 70.0
    if macd_cross == -1.0:  # bearish cross
        return 30.0 if macd_above_zero else 0.0
    return 60.0 if macd_above_zero else 40.0


def _score_price_vs_ma(pct_diff: Optional[float]) -> float:
    """
    Score price relative to a moving average.
    >5% above MA → 80;  At MA (0%) → 50;  >5% below MA → 30
    Linear interpolation between anchors.  Capped at 100 above, 0 below.
    """
    if pct_diff is None:
        return 50.0

    if pct_diff >= 5:
        return min(100.0, 80.0 + (pct_diff - 5) * (20.0 / 15.0))
    if pct_diff >= 0:
        return 50.0 + pct_diff * 6.0
    if pct_diff > -5:
        return 50.0 + pct_diff * 4.0
    return max(0.0, 30.0 - (abs(pct_diff) - 5) * 1.5)


def _score_momentum(
    momentum_20d: Optional[float],
    percentile_rank: Optional[float] = None,
) -> float:
    """
    Score 20-day momentum.
    Prefers percentile rank within population (0–100).
    Falls back to raw return mapping if percentile not available.
    """
    if percentile_rank is not None:
        return float(percentile_rank)
    if momentum_20d is None:
        return 50.0
    score = 50.0 + momentum_20d * 3.0
    return max(0.0, min(100.0, score))


# ── Composite score ───────────────────────────────────────────────────────────

def compute_technical_score(
    indicators: dict,
    market_regime: str = "neutral",
    momentum_percentile: Optional[float] = None,
) -> float:
    """
    Compute weighted composite technical score (0–100).

    Args:
        indicators: dict with keys: rsi_14, macd_cross, macd_above_zero,
                    price_vs_ma50, price_vs_ma200, momentum_20d.
                    Optionally p_up, p_down, prediction_confidence for GBM enrichment.
        market_regime: 'bull' | 'neutral' | 'caution' | 'bear'
        momentum_percentile: percentile rank (0–100) within population for 20d return.

    Returns: float in [0, 100]
    """
    rsi_score = _score_rsi(
        indicators.get("rsi_14", 50.0),
        market_regime=market_regime,
    )
    macd_score = _score_macd(
        indicators.get("macd_cross", 0.0),
        indicators.get("macd_above_zero", False),
    )
    ma50_score = _score_price_vs_ma(indicators.get("price_vs_ma50"))
    ma200_score = _score_price_vs_ma(indicators.get("price_vs_ma200"))
    momentum_score = _score_momentum(
        indicators.get("momentum_20d"),
        percentile_rank=momentum_percentile,
    )

    composite = (
        rsi_score * 0.25
        + macd_score * 0.20
        + ma50_score * 0.20
        + ma200_score * 0.20
        + momentum_score * 0.15
    )

    return round(max(0.0, min(100.0, composite)), 2)


def compute_momentum_percentiles(
    momentum_data: dict[str, Optional[float]],
) -> dict[str, float]:
    """
    Compute percentile ranks for 20d momentum across a universe of tickers.
    Returns {ticker: percentile_rank_0_to_100}.
    """
    valid = [(t, m) for t, m in momentum_data.items() if m is not None]
    if not valid:
        return {t: 50.0 for t in momentum_data}

    tickers, values = zip(*valid)
    values_arr = np.array(values, dtype=float)
    ranks = (values_arr.argsort().argsort() / max(len(values_arr) - 1, 1)) * 100

    result = {t: round(float(r), 1) for t, r in zip(tickers, ranks)}
    for t in momentum_data:
        result.setdefault(t, 50.0)
    return result


# ── Indicator computation from OHLCV ─────────────────────────────────────────
# Uses the same formulas as alpha-engine-predictor/data/feature_engineer.py
# (Wilder's RSI via EWM com=13, MACD 12/26/9, 50/200 SMA, 20d momentum).

def compute_indicators_from_ohlcv(
    price_history: list[dict],
    min_bars: int = 210,
) -> Optional[dict]:
    """
    Compute the 5 technical indicators needed for scoring from OHLCV bars.

    Args:
        price_history: [{date, open, high, low, close}, ...] sorted ascending.
                       Needs at least ~210 bars for 200-day MA.
        min_bars: minimum number of bars required (default 210 for 200-day MA).

    Returns:
        {rsi_14, macd_cross, macd_above_zero, price_vs_ma50, price_vs_ma200, momentum_20d}
        or None if insufficient data.
    """
    if not price_history or len(price_history) < min_bars:
        return None

    close = pd.Series(
        [bar["close"] for bar in price_history],
        dtype=float,
    )

    # ── RSI(14) — Wilder's smoothing via EWM ──
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    rsi_14 = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    # ── MACD (12, 26, 9) ──
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    macd_above_zero = bool(macd_line.iloc[-1] > 0)

    # MACD cross detection (last 3 days)
    diff = macd_line - signal_line
    macd_cross = 0.0
    if len(diff) >= 2:
        for i in range(max(len(diff) - 3, 0), len(diff)):
            if i == 0:
                continue
            if diff.iloc[i] >= 0 and diff.iloc[i - 1] < 0:
                macd_cross = 1.0   # bullish cross
            elif diff.iloc[i] < 0 and diff.iloc[i - 1] >= 0:
                macd_cross = -1.0  # bearish cross

    # ── Price vs 50-day MA ──
    ma50 = close.rolling(50).mean()
    if pd.isna(ma50.iloc[-1]) or ma50.iloc[-1] == 0:
        price_vs_ma50 = None
    else:
        price_vs_ma50 = ((close.iloc[-1] - ma50.iloc[-1]) / ma50.iloc[-1]) * 100

    # ── Price vs 200-day MA ──
    ma200 = close.rolling(200).mean()
    if pd.isna(ma200.iloc[-1]) or ma200.iloc[-1] == 0:
        price_vs_ma200 = None
    else:
        price_vs_ma200 = ((close.iloc[-1] - ma200.iloc[-1]) / ma200.iloc[-1]) * 100

    # ── 20-day momentum ──
    if len(close) >= 21:
        momentum_20d = float((close.iloc[-1] / close.iloc[-21]) - 1) * 100
    else:
        momentum_20d = None

    return {
        "rsi_14": rsi_14,
        "macd_cross": macd_cross,
        "macd_above_zero": macd_above_zero,
        "price_vs_ma50": price_vs_ma50,
        "price_vs_ma200": price_vs_ma200,
        "momentum_20d": momentum_20d,
    }
