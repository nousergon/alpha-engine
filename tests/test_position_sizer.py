"""Unit tests for executor.position_sizer — pure sizing math, no external calls."""
import pytest

from executor.position_sizer import compute_position_size


# ── Helpers ──────────────────────────────────────────────────────────────────


def _base_config(**overrides):
    """Minimal config dict for position sizer."""
    cfg = {
        "max_position_pct": 0.05,
        "conviction_decline_adj": 0.70,
        "min_price_target_upside": 0.05,
        "upside_fail_adj": 0.70,
        "min_position_dollar": 500,
        "sector_adj": {
            "overweight": 1.05,
            "market_weight": 1.00,
            "underweight": 0.85,
        },
        # Disable optional adjustments by default for focused tests
        "atr_sizing_enabled": False,
        "confidence_sizing_enabled": False,
        "staleness_discount_enabled": False,
        "earnings_sizing_enabled": False,
    }
    cfg.update(overrides)
    return cfg


def _base_signal(**overrides):
    """Minimal signal dict."""
    sig = {
        "score": 82,
        "conviction": "stable",
        "price_target_upside": 0.15,
    }
    sig.update(overrides)
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
# compute_position_size
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputePositionSize:

    def test_base_weight_equals_one_over_n(self):
        """With 4 enter signals, base weight = 0.25."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(4)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        # 1/4 = 0.25, capped at max_position_pct=0.05
        assert result["position_pct"] == 0.05

    def test_base_weight_single_entry(self):
        """With 1 entry, base weight = 1.0, capped at max_position_pct=0.05."""
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": "AAPL"}],
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["position_pct"] == 0.05
        assert result["dollar_size"] == 5000.0

    def test_base_weight_many_entries_below_cap(self):
        """With 25 entries, base weight = 0.04 < 0.05 cap."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["position_pct"] == 0.04

    def test_overweight_sector_adjustment(self):
        """Overweight sector should increase weight by 1.05x."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(),
            sector_rating="overweight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["sector_adj"] == 1.05
        # 0.04 * 1.05 = 0.042
        assert result["position_pct"] == 0.042

    def test_underweight_sector_adjustment(self):
        """Underweight sector should decrease weight by 0.85x."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(),
            sector_rating="underweight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["sector_adj"] == 0.85
        # 0.04 * 0.85 = 0.034
        assert result["position_pct"] == 0.034

    def test_declining_conviction_reduces_weight(self):
        """Declining conviction should apply 0.70 multiplier."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(conviction="declining"),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["conviction_adj"] == 0.70
        # 0.04 * 0.70 = 0.028
        assert result["position_pct"] == 0.028

    def test_stable_conviction_no_reduction(self):
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": f"T{i}"} for i in range(25)],
            signal=_base_signal(conviction="stable"),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["conviction_adj"] == 1.0

    def test_rising_conviction_no_reduction(self):
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": f"T{i}"} for i in range(25)],
            signal=_base_signal(conviction="rising"),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["conviction_adj"] == 1.0

    def test_cap_at_max_position_pct(self):
        """Even with all multipliers > 1, position weight should be capped."""
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": "AAPL"}],  # base = 1.0
            signal=_base_signal(conviction="rising"),
            sector_rating="overweight",
            current_price=150.0,
            config=_base_config(),
        )
        # 1.0 * 1.05 * 1.0 * 1.0 = 1.05, capped at 0.05
        assert result["position_pct"] == 0.05

    def test_zero_entries_handled_gracefully(self):
        """Empty enter_signals list should not crash (max(0,1) = 1)."""
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[],
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        # base = 1/max(0,1) = 1.0, capped at 0.05
        assert result["position_pct"] == 0.05
        assert result["shares"] > 0

    def test_zero_price_returns_zero_shares(self):
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": "AAPL"}],
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=0,
            config=_base_config(),
        )
        assert result["shares"] == 0

    def test_drawdown_multiplier_reduces_sizing(self):
        """Passing drawdown_multiplier=0.50 should halve the position."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
            drawdown_multiplier=0.50,
        )
        # 0.04 * 0.50 = 0.02
        assert result["position_pct"] == 0.02
        assert result["dd_multiplier"] == 0.50

    def test_shares_floor_division(self):
        """Shares should be floor(dollar_size / price)."""
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=[{"ticker": "AAPL"}],
            signal=_base_signal(),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        # 5000 / 150 = 33.33 → 33 shares
        assert result["shares"] == 33
        assert result["dollar_size"] == 5000.0

    def test_upside_below_minimum_reduces_weight(self):
        """If price_target_upside < min_price_target_upside, apply penalty."""
        enter_signals = [{"ticker": f"T{i}"} for i in range(25)]
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=100_000,
            enter_signals=enter_signals,
            signal=_base_signal(price_target_upside=0.02),
            sector_rating="market_weight",
            current_price=150.0,
            config=_base_config(),
        )
        assert result["upside_adj"] == 0.70
        # 0.04 * 0.70 = 0.028
        assert result["position_pct"] == 0.028

    def test_min_position_dollar_filters_tiny_orders(self):
        """If dollar_size < min_position_dollar, shares should be 0."""
        result = compute_position_size(
            ticker="AAPL",
            portfolio_nav=5_000,  # small portfolio
            enter_signals=[{"ticker": f"T{i}"} for i in range(25)],
            signal=_base_signal(),
            sector_rating="underweight",
            current_price=150.0,
            config=_base_config(),
            drawdown_multiplier=0.25,
        )
        # base=0.04, sector=0.85, dd=0.25 → 0.04*0.85*0.25=0.0085
        # dollar = 5000 * 0.0085 = 42.50 < 500 min
        assert result["shares"] == 0
