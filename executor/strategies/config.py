"""
Strategy layer configuration.

Defaults are defined here. Override via the 'strategy' key in config/risk.yaml.
"""

from __future__ import annotations

# ── Exit Manager defaults ────────────────────────────────────────────────────
# These are conservative fallbacks — override via strategy key in risk.yaml.

# ATR trailing stop
ATR_TRAILING_ENABLED = True
ATR_PERIOD = 14               # days for ATR calculation
ATR_MULTIPLIER = 2.5          # stop = highest_high - ATR * multiplier

# Time-based exit decay
TIME_DECAY_ENABLED = True
TIME_DECAY_REDUCE_DAYS = 7    # trading days before 50% reduction
TIME_DECAY_EXIT_DAYS = 14     # trading days before full exit

# ── Graduated Drawdown defaults ──────────────────────────────────────────────

GRADUATED_DRAWDOWN_ENABLED = True
DRAWDOWN_TIERS = [
    # (threshold, sizing_multiplier, description)
    (-0.02, 1.00, "0% to -2%: full sizing"),
    (-0.04, 0.50, "-2% to -4%: half sizing"),
    (-0.06, 0.25, "-4% to -6%: quarter sizing"),
    # Beyond circuit breaker threshold: full halt
]


def load_strategy_config(config: dict) -> dict:
    """
    Extract strategy configuration from the main risk.yaml config.

    The 'strategy' key in risk.yaml can override any default.
    Returns a flat dict of strategy parameters.
    """
    strategy = config.get("strategy", {})

    exit_cfg = strategy.get("exit_manager", {})
    drawdown_cfg = strategy.get("graduated_drawdown", {})

    return {
        # ATR trailing stop
        "atr_trailing_enabled": exit_cfg.get("atr_trailing_enabled", ATR_TRAILING_ENABLED),
        "atr_period": exit_cfg.get("atr_period", ATR_PERIOD),
        "atr_multiplier": exit_cfg.get("atr_multiplier", ATR_MULTIPLIER),

        # Time-based exit decay
        "time_decay_enabled": exit_cfg.get("time_decay_enabled", TIME_DECAY_ENABLED),
        "time_decay_reduce_days": exit_cfg.get("time_decay_reduce_days", TIME_DECAY_REDUCE_DAYS),
        "time_decay_exit_days": exit_cfg.get("time_decay_exit_days", TIME_DECAY_EXIT_DAYS),

        # Graduated drawdown
        "graduated_drawdown_enabled": drawdown_cfg.get("enabled", GRADUATED_DRAWDOWN_ENABLED),
        "drawdown_tiers": drawdown_cfg.get("tiers", DRAWDOWN_TIERS),
    }
