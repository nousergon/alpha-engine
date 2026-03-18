"""
Exit manager — generates EXIT and REDUCE signals from quantitative rules.

Five independent exit strategies run in parallel:
  1. ATR trailing stop: exit if price falls below highest_high - ATR * multiplier
  2. Profit-taking: reduce when unrealized gain exceeds threshold
  3. Momentum exit: exit on severe negative momentum + oversold RSI
  4. Time-based decay: reduce after N days, exit after M days without thesis refresh
  5. Sector-relative veto: cancel ATR exit if stock is outperforming its sector

These are additive to Research signals — if Research says HOLD but exit_manager
says EXIT, the exit fires. Research EXIT signals always take precedence.

All logic uses data available at trade time (OHLCV from yfinance or IBKR).
No LLM calls required — fully backtestable.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SECTOR_ETF_MAP = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Industrials": "XLI",
    "Communication Services": "XLC",
}


def check_atr_trailing_stop(
    ticker: str,
    current_price: float,
    entry_date: str,
    price_history: list[dict],
    strategy_config: dict,
) -> dict | None:
    """
    Check if a position should be exited based on ATR trailing stop.

    The trailing stop is: highest_high_since_entry - ATR(period) * multiplier.
    If current_price <= stop_level, return an EXIT signal.

    Args:
        ticker: stock symbol
        current_price: current market price
        entry_date: ISO date string (YYYY-MM-DD) when position was entered
        price_history: list of dicts with keys {date, open, high, low, close},
                       sorted ascending by date. Must cover at least ATR period
                       days before entry_date through today.
        strategy_config: from load_strategy_config()

    Returns:
        EXIT signal dict if stop triggered, else None.
    """
    if not strategy_config.get("atr_trailing_enabled", True):
        return None

    if not price_history or current_price is None:
        return None

    period = strategy_config.get("atr_period", 14)
    multiplier = strategy_config.get("atr_multiplier", 3.0)

    # Filter to bars on or after entry_date
    entry_dt = date.fromisoformat(entry_date)
    post_entry = [b for b in price_history if date.fromisoformat(b["date"]) >= entry_dt]

    if len(post_entry) < 2:
        # Not enough data since entry to compute trailing stop
        return None

    # Compute ATR from the full price history (need period+1 bars minimum)
    atr = _compute_atr(price_history, period)
    if atr is None or atr <= 0:
        return None

    # Highest high since entry
    highest_high = max(b["high"] for b in post_entry)

    stop_level = highest_high - (atr * multiplier)

    if current_price <= stop_level:
        logger.info(
            f"ATR TRAILING STOP triggered for {ticker}: "
            f"price=${current_price:.2f} <= stop=${stop_level:.2f} "
            f"(high=${highest_high:.2f} - ATR={atr:.2f} x {multiplier})"
        )
        return {
            "ticker": ticker,
            "action": "EXIT",
            "reason": "atr_trailing_stop",
            "detail": (
                f"price=${current_price:.2f} <= stop=${stop_level:.2f} "
                f"(highest_high=${highest_high:.2f} - ATR({period})={atr:.2f} x {multiplier})"
            ),
            "stop_level": round(stop_level, 2),
            "atr": round(atr, 2),
            "highest_high": round(highest_high, 2),
        }

    return None


def check_time_decay(
    ticker: str,
    entry_date: str,
    run_date: str,
    signal_action: str,
    strategy_config: dict,
) -> dict | None:
    """
    Check if a position should be reduced or exited based on holding period.

    Only fires if the Research signal is HOLD (not actively recommending
    the position). If Research says ENTER (reaffirming), time decay resets.

    Args:
        ticker: stock symbol
        entry_date: ISO date string when position was entered
        run_date: today's date (ISO string)
        signal_action: Research signal for this ticker today ("ENTER"|"HOLD"|"EXIT"|"REDUCE")
        strategy_config: from load_strategy_config()

    Returns:
        REDUCE or EXIT signal dict if time limit hit, else None.
    """
    if not strategy_config.get("time_decay_enabled", True):
        return None

    # If Research is actively reaffirming (ENTER) or already exiting, skip time decay
    if signal_action in ("ENTER", "EXIT", "REDUCE"):
        return None

    reduce_days = strategy_config.get("time_decay_reduce_days", 5)
    exit_days = strategy_config.get("time_decay_exit_days", 10)

    entry_dt = date.fromisoformat(entry_date)
    run_dt = date.fromisoformat(run_date)
    calendar_days = (run_dt - entry_dt).days

    # Approximate trading days (exclude weekends): ~5 trading days per 7 calendar days
    trading_days = _approx_trading_days(entry_dt, run_dt)

    if trading_days >= exit_days:
        logger.info(
            f"TIME DECAY EXIT for {ticker}: held ~{trading_days} trading days "
            f"(>= {exit_days} day exit threshold)"
        )
        return {
            "ticker": ticker,
            "action": "EXIT",
            "reason": "time_decay_exit",
            "detail": f"held ~{trading_days} trading days (exit threshold: {exit_days})",
            "trading_days_held": trading_days,
        }

    if trading_days >= reduce_days:
        logger.info(
            f"TIME DECAY REDUCE for {ticker}: held ~{trading_days} trading days "
            f"(>= {reduce_days} day reduce threshold)"
        )
        return {
            "ticker": ticker,
            "action": "REDUCE",
            "reason": "time_decay_reduce",
            "detail": f"held ~{trading_days} trading days (reduce threshold: {reduce_days})",
            "trading_days_held": trading_days,
        }

    return None


def check_profit_take(
    ticker: str,
    current_price: float,
    avg_cost: float | None,
    strategy_config: dict,
) -> dict | None:
    """
    Check if a position should be partially sold to lock in gains.

    If unrealized gain exceeds the configured threshold, return a REDUCE signal.

    Args:
        ticker: stock symbol
        current_price: current market price
        avg_cost: average cost basis per share
        strategy_config: from load_strategy_config()

    Returns:
        REDUCE signal dict if profit threshold exceeded, else None.
    """
    if not strategy_config.get("profit_take_enabled", True):
        return None

    if avg_cost is None or avg_cost <= 0:
        return None

    unrealized_gain = (current_price - avg_cost) / avg_cost
    threshold = strategy_config.get("profit_take_pct", 0.25)

    if unrealized_gain >= threshold:
        logger.info(
            f"PROFIT TAKE triggered for {ticker}: "
            f"gain={unrealized_gain:.2%} >= threshold={threshold:.2%}"
        )
        return {
            "ticker": ticker,
            "action": "REDUCE",
            "reason": "profit_take",
            "detail": (
                f"unrealized gain {unrealized_gain:.2%} >= "
                f"threshold {threshold:.2%}"
            ),
            "unrealized_gain": round(unrealized_gain, 4),
        }

    return None


def check_sector_relative_veto(
    ticker: str,
    sector: str,
    price_history: list[dict],
    sector_etf_history: list[dict],
    strategy_config: dict,
) -> bool:
    """
    Veto an exit if the stock is outperforming its sector ETF.

    If the stock's recent return exceeds the sector ETF return by more than
    the configured threshold, the exit should be vetoed (stock still has
    relative momentum).

    Args:
        ticker: stock symbol
        sector: sector name (used for logging only)
        price_history: stock OHLCV bars sorted ascending
        sector_etf_history: sector ETF OHLCV bars sorted ascending
        strategy_config: from load_strategy_config()

    Returns:
        True if exit should be vetoed, False otherwise.
    """
    if not strategy_config.get("sector_relative_veto_enabled", True):
        return False

    if not price_history or len(price_history) < 5:
        return False

    if not sector_etf_history or len(sector_etf_history) < 5:
        return False

    lookback = min(20, len(price_history), len(sector_etf_history))

    stock_return = (
        price_history[-1]["close"] / price_history[-lookback]["close"]
    ) - 1
    sector_return = (
        sector_etf_history[-1]["close"] / sector_etf_history[-lookback]["close"]
    ) - 1

    outperformance = stock_return - sector_return
    threshold = strategy_config.get("sector_relative_outperform_threshold", 0.05)

    if outperformance > threshold:
        logger.warning(
            f"SECTOR VETO for {ticker}: outperforming {sector} by "
            f"{outperformance:.2%} (threshold={threshold:.2%}) — exit vetoed"
        )
        return True

    return False


def check_momentum_exit(
    ticker: str,
    price_history: list[dict],
    strategy_config: dict,
) -> dict | None:
    """
    Check if a position should be exited based on severe negative momentum.

    Triggers when both 20-day momentum is deeply negative AND RSI is oversold,
    indicating a sustained downtrend with no reversal signal.

    Args:
        ticker: stock symbol
        price_history: OHLCV bars sorted ascending (needs >= 21 bars)
        strategy_config: from load_strategy_config()

    Returns:
        EXIT signal dict if momentum criteria met, else None.
    """
    if not strategy_config.get("momentum_exit_enabled", True):
        return None

    if price_history is None or len(price_history) < 21:
        return None

    # 20-day momentum (percentage)
    momentum = (price_history[-1]["close"] / price_history[-21]["close"] - 1) * 100

    # RSI(14)
    rsi = _compute_rsi(price_history, period=14)

    mom_threshold = strategy_config.get("momentum_exit_threshold", -15.0)
    rsi_threshold = strategy_config.get("momentum_exit_rsi", 30)

    if momentum < mom_threshold and rsi is not None and rsi < rsi_threshold:
        logger.info(
            f"MOMENTUM EXIT triggered for {ticker}: "
            f"20d momentum={momentum:.1f}% (< {mom_threshold}%), "
            f"RSI={rsi:.1f} (< {rsi_threshold})"
        )
        return {
            "ticker": ticker,
            "action": "EXIT",
            "reason": "momentum_exit",
            "detail": (
                f"20d momentum={momentum:.1f}% (threshold={mom_threshold}%), "
                f"RSI(14)={rsi:.1f} (threshold={rsi_threshold})"
            ),
        }

    return None


def evaluate_exits(
    current_positions: dict[str, dict],
    signals_by_ticker: dict[str, dict],
    run_date: str,
    price_histories: dict[str, list[dict]],
    ibkr_client,
    strategy_config: dict,
    sector_etf_histories: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """
    Evaluate all held positions against exit rules.

    Returns a list of strategy-generated EXIT/REDUCE signals. These are
    merged with Research signals in main.py — strategy exits supplement
    Research exits (they don't conflict).

    Check order:
      1. ATR trailing stop (with sector-relative veto)
      2. Profit-taking
      3. Momentum exit
      4. Time-based decay

    Args:
        current_positions: {ticker: {shares, market_value, avg_cost, sector, entry_date}}
        signals_by_ticker: {ticker: signal_dict} from Research
        run_date: today's date
        price_histories: {ticker: [{date, open, high, low, close}, ...]}
        ibkr_client: for fetching current prices
        strategy_config: from load_strategy_config()
        sector_etf_histories: {etf_ticker: [{date, open, high, low, close}, ...]}
                              for sector-relative veto. None disables veto.

    Returns:
        List of signal dicts with action="EXIT" or "REDUCE" and reason field.
    """
    strategy_signals = []

    for ticker, pos in current_positions.items():
        entry_date = pos.get("entry_date")
        if not entry_date:
            continue

        research_signal = signals_by_ticker.get(ticker, {})
        research_action = research_signal.get("signal", "HOLD")

        # Skip if Research is already exiting this position
        if research_action in ("EXIT", "REDUCE"):
            continue

        current_price = ibkr_client.get_current_price(ticker)
        if current_price is None:
            continue

        history = price_histories.get(ticker, [])

        # 1. ATR trailing stop (with sector-relative veto)
        atr_signal = check_atr_trailing_stop(
            ticker=ticker,
            current_price=current_price,
            entry_date=entry_date,
            price_history=history,
            strategy_config=strategy_config,
        )
        if atr_signal:
            # Check sector-relative veto before accepting ATR exit
            sector = pos.get("sector", "")
            etf_ticker = SECTOR_ETF_MAP.get(sector, "SPY")
            etf_history = (
                sector_etf_histories.get(etf_ticker, [])
                if sector_etf_histories
                else []
            )
            if check_sector_relative_veto(
                ticker, sector, history, etf_history, strategy_config
            ):
                logger.info(
                    f"ATR exit for {ticker} vetoed — outperforming sector ({sector})"
                )
            else:
                strategy_signals.append(atr_signal)
                continue  # ATR exit takes priority over other checks

        # 2. Profit-taking
        avg_cost = pos.get("avg_cost")
        profit_signal = check_profit_take(
            ticker=ticker,
            current_price=current_price,
            avg_cost=avg_cost,
            strategy_config=strategy_config,
        )
        if profit_signal:
            strategy_signals.append(profit_signal)
            continue  # Profit-take fires, skip remaining checks

        # 3. Momentum exit
        momentum_signal = check_momentum_exit(
            ticker=ticker,
            price_history=history,
            strategy_config=strategy_config,
        )
        if momentum_signal:
            strategy_signals.append(momentum_signal)
            continue  # Momentum exit fires, skip time decay

        # 4. Time-based decay
        time_signal = check_time_decay(
            ticker=ticker,
            entry_date=entry_date,
            run_date=run_date,
            signal_action=research_action,
            strategy_config=strategy_config,
        )
        if time_signal:
            strategy_signals.append(time_signal)

    return strategy_signals


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compute_atr(price_history: list[dict], period: int = 14) -> float | None:
    """
    Compute Average True Range over the last `period` bars.

    Uses Wilder's smoothing (EWM with alpha=1/period).
    Returns None if insufficient data.
    """
    if len(price_history) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(price_history)):
        bar = price_history[i]
        prev_close = price_history[i - 1]["close"]
        high = bar["high"]
        low = bar["low"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    # Wilder's smoothed ATR: start with SMA, then EWM
    atr = sum(true_ranges[:period]) / period
    alpha = 1.0 / period
    for tr in true_ranges[period:]:
        atr = atr * (1 - alpha) + tr * alpha

    return atr


def _compute_rsi(price_history: list[dict], period: int = 14) -> float | None:
    """
    Compute Relative Strength Index over the last `period` bars.

    Uses Wilder's smoothing (same as ATR) for average gain/loss.
    Returns None if insufficient data.
    """
    if len(price_history) < period + 1:
        return None

    # Close-to-close changes
    changes = [
        price_history[i]["close"] - price_history[i - 1]["close"]
        for i in range(1, len(price_history))
    ]

    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]

    if len(gains) < period:
        return None

    # Initial SMA for first `period` bars
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for remaining bars
    alpha = 1.0 / period
    for i in range(period, len(gains)):
        avg_gain = avg_gain * (1 - alpha) + gains[i] * alpha
        avg_loss = avg_loss * (1 - alpha) + losses[i] * alpha

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _approx_trading_days(start: date, end: date) -> int:
    """
    Approximate trading days between two dates (excludes weekends).
    Does not account for market holidays — close enough for decay logic.
    """
    if end <= start:
        return 0
    total = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Monday=0 through Friday=4
            total += 1
    return total
