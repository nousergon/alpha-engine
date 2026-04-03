"""Unit tests for executor.signal_generator — prediction validation and circuit breaker."""
import pytest

from executor.signal_generator import (
    _validate_predictions,
    _compute_gbm_adjustment,
    generate_trading_signals,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_prediction(
    ticker="AAPL",
    direction="UP",
    confidence=0.65,
    p_up=0.55,
    p_down=0.20,
    p_flat=0.25,
    **overrides,
):
    pred = {
        "ticker": ticker,
        "predicted_direction": direction,
        "prediction_confidence": confidence,
        "p_up": p_up,
        "p_down": p_down,
        "p_flat": p_flat,
    }
    pred.update(overrides)
    return pred


def _make_predictions(n=20, direction="UP", confidence=0.65, p_up=0.55, p_down=0.20, p_flat=0.25):
    """Generate n predictions with varied tickers."""
    return {
        f"TICK{i}": _make_prediction(
            ticker=f"TICK{i}",
            direction=direction,
            confidence=confidence,
            p_up=p_up,
            p_down=p_down,
            p_flat=p_flat,
        )
        for i in range(n)
    }


def _diverse_predictions(n=20):
    """Generate predictions with diverse directions and confidences."""
    preds = {}
    directions = ["UP", "DOWN", "FLAT"]
    for i in range(n):
        d = directions[i % 3]
        conf = 0.50 + (i % 10) * 0.03  # 0.50 to 0.77
        p_up = 0.45 + (i % 5) * 0.05   # varied
        p_down = 0.20 + (i % 4) * 0.05
        p_flat = max(0.0, 1.0 - p_up - p_down)
        preds[f"TICK{i}"] = _make_prediction(
            ticker=f"TICK{i}",
            direction=d,
            confidence=conf,
            p_up=p_up,
            p_down=p_down,
            p_flat=p_flat,
        )
    return preds


def _base_config(**overrides):
    cfg = {
        "trading": {
            "min_technical_score": 60,
            "gbm_veto_confidence": 0.65,
            "gbm_enrichment_max": 10.0,
            "exit_score_threshold": 30,
        },
    }
    cfg.update(overrides)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# _validate_predictions
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidatePredictions:
    def test_diverse_predictions_valid(self):
        result = _validate_predictions(_diverse_predictions(20))
        assert result["valid"] is True
        assert result["reasons"] == []

    def test_empty_predictions_valid(self):
        result = _validate_predictions({})
        assert result["valid"] is True

    def test_direction_clustering_triggers(self):
        """90% same direction should trigger circuit breaker."""
        preds = _make_predictions(20, direction="UP")
        result = _validate_predictions(preds)
        assert result["valid"] is False
        assert any("Direction clustering" in r for r in result["reasons"])

    def test_direction_clustering_at_boundary(self):
        """Exactly 80% should not trigger (> 80% required)."""
        preds = {}
        for i in range(16):  # 80% UP
            preds[f"UP{i}"] = _make_prediction(
                ticker=f"UP{i}", direction="UP", confidence=0.50 + i * 0.02,
            )
        for i in range(4):   # 20% DOWN
            preds[f"DN{i}"] = _make_prediction(
                ticker=f"DN{i}", direction="DOWN", confidence=0.55 + i * 0.03,
            )
        result = _validate_predictions(preds)
        assert result["valid"] is True

    def test_confidence_clustering_triggers(self):
        """All confidences at 0.51 should trigger circuit breaker."""
        preds = _make_predictions(20, confidence=0.51)
        # Need diverse directions to avoid direction clustering
        directions = ["UP", "DOWN", "FLAT"]
        for i, (ticker, pred) in enumerate(preds.items()):
            pred["predicted_direction"] = directions[i % 3]
        result = _validate_predictions(preds)
        assert result["valid"] is False
        assert any("Confidence clustering" in r for r in result["reasons"])

    def test_small_set_skips_clustering_checks(self):
        """Fewer than 5 predictions should not trigger clustering."""
        preds = _make_predictions(3, direction="UP", confidence=0.51)
        result = _validate_predictions(preds)
        assert result["valid"] is True

    def test_out_of_bounds_clamped(self):
        """p_up=2.0 should be clamped to 1.0."""
        preds = {"AAPL": _make_prediction(p_up=2.0, p_down=-0.5, confidence=1.5)}
        result = _validate_predictions(preds)
        assert result["clamped"] == 3  # p_up, p_down, confidence
        assert preds["AAPL"]["p_up"] == 1.0
        assert preds["AAPL"]["p_down"] == 0.0
        assert preds["AAPL"]["prediction_confidence"] == 1.0

    def test_probability_sum_warning(self):
        """Large probability sum deviation triggers warning."""
        preds = _make_predictions(10, p_up=0.8, p_down=0.7, p_flat=0.5)
        # Need diverse directions
        directions = ["UP", "DOWN", "FLAT"]
        for i, pred in enumerate(preds.values()):
            pred["predicted_direction"] = directions[i % 3]
            pred["prediction_confidence"] = 0.5 + i * 0.03
        result = _validate_predictions(preds)
        assert any("Probability sum" in r for r in result["reasons"])


# ═══════════════════════════════════════════════════════════════════════════════
# _compute_gbm_adjustment
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeGbmAdjustment:
    def test_empty_prediction_returns_zero(self):
        assert _compute_gbm_adjustment({}, 10.0) == 0.0

    def test_positive_signal(self):
        pred = {"p_up": 0.7, "p_down": 0.1, "prediction_confidence": 0.8}
        result = _compute_gbm_adjustment(pred, 10.0)
        # (0.7 - 0.1) * 10.0 * 0.8 = 4.8
        assert result == 4.8

    def test_negative_signal(self):
        pred = {"p_up": 0.1, "p_down": 0.7, "prediction_confidence": 0.8}
        result = _compute_gbm_adjustment(pred, 10.0)
        assert result == -4.8

    def test_out_of_bounds_clamped(self):
        """p_up=2.0 should be clamped to 1.0 before computation."""
        pred = {"p_up": 2.0, "p_down": 0.0, "prediction_confidence": 1.5}
        result = _compute_gbm_adjustment(pred, 10.0)
        # After clamping: (1.0 - 0.0) * 10.0 * 1.0 = 10.0
        assert result == 10.0

    def test_none_values_return_zero(self):
        pred = {"p_up": None, "p_down": None}
        assert _compute_gbm_adjustment(pred, 10.0) == 0.0

    def test_result_bounded_by_max_enrichment(self):
        pred = {"p_up": 1.0, "p_down": 0.0, "prediction_confidence": 1.0}
        result = _compute_gbm_adjustment(pred, 5.0)
        assert result <= 5.0


# ═══════════════════════════════════════════════════════════════════════════════
# generate_trading_signals with degenerate predictions
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateTradingSignalsCircuitBreaker:
    def test_degenerate_predictions_fall_back(self):
        """When all predictions are UP, circuit breaker should zero out GBM."""
        population = [
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "conviction": "stable",
                "long_term_rating": "BUY",
                "long_term_score": 80.0,
                "price_target_upside": 0.15,
                "thesis_summary": "test",
            }
        ]
        # All-UP degenerate predictions
        degenerate_preds = _make_predictions(20, direction="UP", confidence=0.70)
        # Add AAPL specifically
        degenerate_preds["AAPL"] = _make_prediction(
            ticker="AAPL", direction="UP", confidence=0.70, p_up=0.7, p_down=0.1
        )

        price_histories = {
            "AAPL": [
                {"date": f"2026-03-{d:02d}", "open": 150 + d, "high": 155 + d,
                 "low": 148 + d, "close": 152 + d, "volume": 1000000}
                for d in range(1, 32)
            ]
        }

        result = generate_trading_signals(
            population=population,
            predictions=degenerate_preds,
            price_histories=price_histories,
            market_regime="neutral",
            sector_ratings={},
            config=_base_config(),
        )

        # Signal should still be generated (just without GBM enrichment)
        all_signals = result["universe"] + result["buy_candidates"]
        assert len(all_signals) == 1
        aapl = all_signals[0]
        # GBM adjustment should be 0 (predictions emptied by circuit breaker)
        assert aapl.get("gbm_adjustment") in (None, 0.0)
