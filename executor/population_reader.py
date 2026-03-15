"""
Read the investment population from S3.

The population is produced weekly by Research and contains 20-25 stocks
selected from S&P 900 with sector-balanced allocation.  Each stock has a
long_term_score (6-12 month thesis quality) and long_term_rating.

Falls back to signals.json universe if population/latest.json doesn't
exist yet (backward compatibility during rollout).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def read_population(s3_bucket: str, max_lookback: int = 10) -> dict:
    """
    Read population/latest.json from S3.

    Falls back to signals.json universe if population file doesn't exist.
    The signals.json fallback extracts long_term_score and long_term_rating
    from existing universe entries (these fields already exist in signals.json).

    Args:
        s3_bucket: S3 bucket name (e.g., 'alpha-engine-research')
        max_lookback: max calendar days to look back for signals.json fallback

    Returns:
        {
            "date": "2026-03-09",
            "market_regime": "neutral",
            "sector_ratings": {...},
            "population": [
                {
                    "ticker": "AAPL",
                    "long_term_score": 72.1,
                    "long_term_rating": "BUY",
                    "sector": "Technology",
                    "conviction": "stable",
                    "price_target_upside": 0.18,
                    "thesis_summary": "...",
                    "sub_scores": {"news_lt": 70.0, "research_lt": 68.0}
                },
                ...
            ]
        }

    Raises:
        RuntimeError: if neither population nor signals.json found.
    """
    s3 = boto3.client("s3")

    # ── Try population/latest.json first ──
    try:
        obj = s3.get_object(Bucket=s3_bucket, Key="population/latest.json")
        data = json.loads(obj["Body"].read())
        pop = data.get("population", [])
        logger.info(
            "Population loaded from population/latest.json | "
            "date=%s | n_stocks=%d | regime=%s",
            data.get("date", "unknown"),
            len(pop),
            data.get("market_regime", "unknown"),
        )
        return data
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.info(
                "population/latest.json not found, falling back to signals.json"
            )
        else:
            raise

    # ── Fallback: read signals.json and extract universe ──
    return _fallback_to_signals(s3, s3_bucket, max_lookback)


def _fallback_to_signals(s3, s3_bucket: str, max_lookback: int) -> dict:
    """
    Read signals.json and convert universe entries to population format.
    Uses long_term_score and long_term_rating fields (already present in
    signals.json output from aggregate_all()).
    """
    start = date.today()

    for days_back in range(max_lookback + 1):
        candidate_date = start - timedelta(days=days_back)
        if candidate_date.weekday() >= 5:
            continue
        key = f"signals/{candidate_date}/signals.json"
        try:
            obj = s3.get_object(Bucket=s3_bucket, Key=key)
            signals = json.loads(obj["Body"].read())
            break
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                continue
            raise
    else:
        raise RuntimeError(
            f"No population or signals found within {max_lookback} days of {start}. "
            "Check that Research has run recently."
        )

    # Convert signals.json universe + buy_candidates to population format
    universe = signals.get("universe", [])
    candidates = signals.get("buy_candidates", [])

    # Deduplicate (candidates take precedence)
    seen: set[str] = set()
    all_stocks: list[dict] = []
    for s in candidates + universe:
        ticker = s.get("ticker")
        if ticker and ticker not in seen:
            seen.add(ticker)
            all_stocks.append(s)

    population = []
    for s in all_stocks:
        sub_scores = s.get("sub_scores", {})
        population.append({
            "ticker": s["ticker"],
            "long_term_score": s.get("long_term_score", s.get("score", 50.0)),
            "long_term_rating": s.get("long_term_rating", s.get("rating", "HOLD")),
            "sector": s.get("sector", "Unknown"),
            "conviction": s.get("conviction", "stable"),
            "price_target_upside": s.get("price_target_upside"),
            "thesis_summary": s.get("thesis_summary", ""),
            "sub_scores": {
                "news_lt": sub_scores.get("news_lt", sub_scores.get("news", 50.0)),
                "research_lt": sub_scores.get("research_lt", sub_scores.get("research", 50.0)),
            },
        })

    result = {
        "date": str(candidate_date),
        "market_regime": signals.get("market_regime", "neutral"),
        "sector_ratings": signals.get("sector_ratings", {}),
        "population": population,
    }

    logger.info(
        "Population from signals.json fallback | date=%s | n_stocks=%d | regime=%s",
        result["date"],
        len(population),
        result["market_regime"],
    )
    return result
