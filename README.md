# Nous Ergon: Alpha Engine

**See the Nous Ergon blog series on [Hashnode](https://nous-ergon.hashnode.dev/)**

**Nous Ergon** (νοῦς ἔργον — "intelligence at work") is a fully autonomous trading system that combines AI-driven research, quantitative prediction, and rule-based execution to generate market alpha.

```
Alpha = Portfolio Return − SPY Return
```

The system targets sustained outperformance against the S&P 500 by splitting the problem into three layers, each matched to the right tool:

| Layer | Tool | Role |
|-------|------|------|
| **Research** | LLM agents (Claude) | Judgment over unstructured data — news, analyst reports, macro context |
| **Prediction** | Machine learning (LightGBM) | Pattern recognition over structured numerical features |
| **Execution** | Deterministic rules | Hard risk constraints that never get creative |

---

## System Architecture

Five modules run on AWS, connected through a shared S3 bucket. Each module reads its inputs from S3 and writes its outputs back — no shared state beyond the bucket.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WEEKLY CADENCE (Monday)                          │
│                                                                     │
│  Research ──── scan 900 tickers, rotate population, write signals  │
│  Predictor Training ──── retrain on multi-year history, promote    │
│  Backtester ──── signal quality + weight optimization + param sweep │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    DAILY CADENCE (Mon–Fri)                           │
│                                                                     │
│  Predictor (6:15 AM PT) ──── reads latest signals.json from S3     │
│       │                                                             │
│       ▼  predictions.json                                           │
│  Executor (6:30 AM PT) ──── trades ───► Interactive Brokers        │
│       │                                                             │
│       ▼  (market close)                                             │
│  EOD Reconcile (1:05 PM PT) ──── NAV, return, alpha ───► email     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    ALWAYS-ON                                        │
│                                                                     │
│  Dashboard (Streamlit) ──── read-only monitoring via S3             │
└─────────────────────────────────────────────────────────────────────┘
```

S3 as the communication bus means any module can be replaced, rewritten, or tested independently. They agree on a JSON schema, and S3 handles the rest.

---

## Modules

### 1. Research — [`alpha-engine-research`](https://github.com/cipher813/alpha-engine-research)

Autonomous investment research pipeline. Five LLM agents orchestrated by LangGraph maintain rolling investment theses on a configurable universe of tracked stocks and scan ~900 S&P 500/400 tickers weekly for top buy candidates.

- Quantitative filter reduces ~900 tickers to a shortlist (no LLM calls)
- Ranking agent (Sonnet) selects top candidates from the filtered set
- Per-ticker agents (news + research) run independently on every candidate (Haiku)
- Macro agent (Sonnet) assesses market environment and sector conditions
- Consolidator (Sonnet) synthesizes into a morning research brief via email
- Outputs composite attractiveness scores (0–100) per ticker as `signals.json`

### 2. Predictor — [`alpha-engine-predictor`](https://github.com/cipher813/alpha-engine-predictor)

LightGBM model that predicts 5-day market-relative returns for each ticker. Produces directional predictions (UP/FLAT/DOWN) with confidence scores.

- Engineered features across technical indicators, macro context, volume, and cross-sectional measures
- Trains on sector-neutral labels (stock returns minus sector ETF returns)
- Weekly retraining with walk-forward validation; weights promote only if IC gate passes
- Veto gate: high-confidence DOWN predictions override BUY signals from Research

### 3. Executor — [`alpha-engine`](https://github.com/cipher813/alpha-engine) *(this repo)*

Reads signals and predictions from S3, applies hard risk rules, sizes positions, and executes market orders on Interactive Brokers (paper trading).

- Graduated drawdown response with configurable halt threshold
- ATR-based trailing stops (volatility-adaptive) with time-decay exit rules
- Configurable position caps, sector limits, and equity exposure limits
- Deterministic execution — no reasoning, no prediction, just parameter application
- Auto-tuned by backtester via S3-delivered `config/executor_params.json`

### 4. Backtester — [`alpha-engine-backtester`](https://github.com/cipher813/alpha-engine-backtester)

The system's learning mechanism. Validates signal quality, runs attribution analysis, and autonomously recommends parameter updates that flow back to upstream modules.

- Signal quality: measures BUY signal accuracy at configurable horizons
- Attribution: correlates sub-scores with outperformance outcomes
- Weight optimization: adjusts Research scoring weights with conservative guardrails
- Parameter sweep: randomized search across executor parameters, ranked by Sharpe ratio
- Veto threshold calibration: sweeps predictor confidence thresholds

### 5. Dashboard — [`alpha-engine-dashboard`](https://github.com/cipher813/alpha-engine-dashboard)

Read-only Streamlit application for monitoring the full system: portfolio performance vs SPY, signal quality trends, per-ticker research timelines, backtester results, and predictor metrics.

---

## Getting Started

Each module has its own README with a Quick Start section. The table below shows what you need to configure for each:

| Module | Config Files to Create | First Command |
|--------|----------------------|---------------|
| [Research](https://github.com/cipher813/alpha-engine-research) | `.env`, `config/universe.yaml`, `config/scoring.yaml`, `config/prompts/` + 13 proprietary source files | `python3 main.py --dry-run --skip-scanner` |
| [Predictor](https://github.com/cipher813/alpha-engine-predictor) | `config/predictor.yaml` | `python train_gbm.py --data-dir data/cache` |
| [Executor](https://github.com/cipher813/alpha-engine) | `config/risk.yaml` | `python executor/main.py --dry-run` |
| [Backtester](https://github.com/cipher813/alpha-engine-backtester) | `config.yaml` | `python backtest.py --mode signal-quality` |
| [Dashboard](https://github.com/cipher813/alpha-engine-dashboard) | None (works with defaults) | `streamlit run app.py` |

---

## Executor Quick Start (This Repo)

### Prerequisites

- Python 3.11+
- IB Gateway running in paper mode on port 4002
- AWS credentials with S3 read/write and SES send permission

### Setup

```bash
git clone https://github.com/cipher813/alpha-engine.git
cd alpha-engine
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/risk.yaml.example config/risk.yaml
# Edit config/risk.yaml — set S3 bucket names, email addresses, risk parameters

python executor/connection_test.py   # verify IB Gateway
python executor/main.py --dry-run    # full loop, no orders placed
```

### Key Files

```
executor/main.py              # Daily trading loop (entry point)
executor/signal_reader.py     # Reads signals.json from S3
executor/risk_guard.py        # Hard rule enforcement + graduated drawdown
executor/position_sizer.py    # Equal-weight base with adjustments
executor/ibkr.py              # IB Gateway wrapper (ib_insync)
executor/strategies/          # ATR trailing stops + time-decay exits
executor/eod_reconcile.py     # EOD P&L vs SPY + email
config/risk.yaml.example      # Safe template — copy to config/risk.yaml
```

---

## Executor Architecture

### Daily Execution Flow

The executor is **not a continuous trading system**. It runs a single ~5-minute pass at market open, places all orders, and exits. No intraday monitoring, no price checks, no order adjustments.

| Step | Time (ET) | What happens |
|------|-----------|--------------|
| **Executor** | 9:30 AM | Single morning pass — read signals, evaluate exits, apply risk rules, size positions, place all orders |
| **EOD Reconcile** | 4:05 PM | Capture final NAV, compute daily return vs SPY, log alpha, send email report |

### Decision Pipeline

Every ENTER signal flows through this deterministic pipeline:

```
signals.json (S3)
       │
       ▼
Signal Reader ──── read today's signals; fall back up to 5 prior trading days
       │
       ▼
Exit Manager ──── evaluate held positions against ATR stops + time decay
       │
       ▼
Risk Guard ──── 7 rule layers (all must pass):
  1. Score minimum (score >= min_score_to_enter, default 70)
  2. Conviction gate (blocks "declining" conviction)
  3. Graduated drawdown (tiered sizing reduction → halt at circuit breaker)
  4. Max single position (% of NAV, adjustable by regime)
  5. Bear regime block (blocks new entries in underweight sectors)
  6. Sector exposure limit (default 25% NAV)
  7. Max total equity (default 90% NAV)
       │
       ▼
Position Sizer ──── compute shares:
  base_weight = 1 / n_enter_signals
  × sector_adj (overweight 1.05, market 1.0, underweight 0.85)
  × conviction_adj (declining → 0.70)
  × upside_adj (< min_upside → 0.70)
  × confidence_adj (from GBM p_up, configurable tiers)
  × atr_adj (inverse volatility scaling, if enabled)
  × drawdown_multiplier (from graduated drawdown tier)
  → capped at max_position_pct
       │
       ▼
IBKR ──── place BUY/SELL market orders on paper account (port 4002)
       │
       ▼
Trade Logger ──── persist to SQLite + S3 backup
```

### Data Sources

The executor consumes six data streams — all read-only, no feedback during execution:

| Source | What | Updated |
|--------|------|---------|
| `signals/{date}/signals.json` | Per-ticker signal (ENTER/EXIT/REDUCE/HOLD), score, conviction, price target, sector rating | Weekly (Monday, by Research) |
| `predictor/predictions/{date}.json` | Per-ticker predicted direction, confidence, predicted alpha | Daily (by Predictor) |
| IBKR account state | Live NAV, positions, current prices | Real-time via IB Gateway |
| `predictor/price_cache_slim/*.parquet` | 2-year OHLCV per ticker (for ATR computation) | Weekly |
| `trades.db` (SQLite) | Peak NAV, entry dates, trade history | After each execution |
| `config/executor_params.json` (S3) | Backtester-tuned parameters | Weekly (Monday, by Backtester) |

### Exit Strategies

Three backtestable exit rules run independently on held positions:

| Strategy | Trigger | Default | Config key |
|----------|---------|---------|------------|
| **ATR trailing stop** | Price falls below `highest_high - ATR(period) × multiplier` | period=14, multiplier=2.5 | `strategy.exit_manager.atr_*` |
| **Time-based decay** | Position held > N trading days AND Research signal is HOLD (not reaffirming) | reduce at 7d, exit at 14d | `strategy.exit_manager.time_decay_*` |
| **Graduated drawdown** | Portfolio drawdown exceeds tier thresholds | 1.0× at -2%, 0.5× at -4%, 0.25× at -6%, halt at -8% | `strategy.graduated_drawdown.*` |

EXIT and REDUCE signals from Research always bypass all risk rules — reducing exposure is never blocked.

### EC2 Infrastructure

The executor shares an EC2 instance with other system components:

| Process | Type | Schedule | Port |
|---------|------|----------|------|
| Nginx (reverse proxy + SSL) | Always-on | 24/7 | 80, 443 |
| nousergon.ai (Streamlit) | Always-on | 24/7 | 8502 |
| dashboard.nousergon.ai (Streamlit) | Always-on | 24/7 | 8501 |
| IB Gateway (paper trading) | Always-on | 24/7 | 4002 |
| Executor (`main.py`) | Cron | 9:30 AM ET weekdays | — |
| EOD Reconcile (`eod_reconcile.py`) | Cron | 4:05 PM ET weekdays | — |
| Backtester | Cron | Monday 3:00 AM ET | — |

The executor uses ~7 minutes of compute per day (two cron jobs). The instance runs 24/7 to serve the public website and dashboard.

---

## Auto-Optimization

The backtester writes three S3 config files that upstream modules read on cold-start, closing the feedback loop automatically:

| S3 Key | Written By | Read By | Controls |
|--------|-----------|---------|----------|
| `config/scoring_weights.json` | Backtester | Research | Sub-score composite weights |
| `config/executor_params.json` | Backtester | Executor | Risk parameters and sizing |
| `config/predictor_params.json` | Backtester | Predictor | Veto confidence threshold |

---

## Key Metrics

| Metric | What It Measures |
|--------|-----------------|
| Total alpha | Portfolio cumulative return − SPY cumulative return |
| Sharpe ratio | Risk-adjusted return (annualized) |
| Daily alpha | Portfolio daily return − SPY daily return |
| Signal accuracy | % of BUY signals beating SPY over configurable windows |
| GBM IC | Rank correlation of predicted vs actual forward returns |
| Max drawdown | Peak-to-trough portfolio decline |

---

## S3 Layout

All inter-module communication flows through a single S3 bucket:

```
s3://alpha-engine-research/
├── signals/{date}/signals.json          ← Research → Predictor + Executor
├── archive/universe/{TICKER}/           ← Research theses
├── archive/candidates/{TICKER}/         ← Buy candidate theses
├── archive/macro/                       ← Macro environment reports
├── predictor/
│   ├── price_cache/*.parquet            ← 10y OHLCV (weekly refresh)
│   ├── price_cache_slim/*.parquet       ← 2y slice for inference
│   ├── daily_closes/{date}.parquet      ← Daily OHLCV archive
│   ├── weights/gbm_latest.txt           ← Active GBM model
│   ├── predictions/{date}.json          ← Daily predictions
│   └── metrics/latest.json              ← Model performance
├── trades/
│   ├── trades_full.csv                  ← Complete trade audit log
│   └── eod_pnl.csv                      ← Daily NAV, return, alpha
├── backtest/{date}/                     ← Weekly backtester outputs
├── config/scoring_weights.json          ← Auto-updated by Backtester → Research
├── config/executor_params.json          ← Auto-updated by Backtester → Executor
├── config/predictor_params.json         ← Auto-updated by Backtester → Predictor
└── research.db                          ← SQLite (signal history, theses)
```

---

## Stack

| Component | Technology |
|-----------|------------|
| LLM provider | Anthropic Claude (Haiku for per-ticker, Sonnet for synthesis) |
| ML framework | LightGBM |
| Agent orchestration | LangGraph |
| Broker | Interactive Brokers (paper account via IB Gateway) |
| Cloud | AWS (Lambda, S3, SES, EC2) |
| Dashboard | Streamlit + Plotly |
| Databases | SQLite per-module (backed up to S3) |

---

## Opportunities for Improvement

### Execution Quality

- **No connection heartbeat** — IB Gateway connection is created once at startup. If Gateway restarts mid-execution, the executor crashes with no recovery. Plan: add connection health check before each order block, with automatic reconnect on failure.

### Risk Management

- **No volatility-adjusted position sizing** — position sizes don't scale with VIX or realized volatility. Plan: add ATR-based sizing layer (`risk_per_trade / (ATR_14 * price)`) that naturally sizes smaller in volatile names.
- **No cross-ticker correlation monitoring** — hidden concentration risk when multiple correlated positions are held (e.g., MSFT + AAPL + GOOGL all correlated >0.8). Plan: compute pairwise rolling correlations and alert or reduce when portfolio-level correlation exceeds a threshold.
- **No profit-taking mechanism** — positions that gain 25%+ have no trim mechanism. Plan: add configurable profit-taking rules (e.g., trim 25% at +20%, trim 50% at +30%).
- **Graduated drawdown only adjusts new entry sizing** — existing positions are untouched during drawdowns. Plan: add forced exit of lowest-conviction holdings when drawdown exceeds a configurable threshold, raising cash for recovery.
- **Confidence-weighted sizing from predictor** — currently the predictor veto is binary (block or pass). Plan: map `p_up` to a continuous sizing multiplier (e.g., p_up 0.50-0.55 = 0.25x, 0.75+ = 1.0x) to extract more value from the ML signal.

### Entry/Exit Strategy

- **No entry momentum confirmation gate** — Plan: require 5d momentum > 0 and price above 20d MA before entering, reducing bad entries by an estimated 15-25%.
- **No sector-relative exit** — ATR stop is absolute price-based. A stock dropping -15% while its sector drops -20% is actually outperforming. Plan: add sector-relative exit option that triggers only when the stock underperforms its sector.
- **No momentum-based exit** — no trigger when 20-day momentum flips negative or RSI drops below 30.
- **REDUCE always sells exactly 50%** — no configuration for partial reduction amounts. Plan: make reduction percentage configurable.

### Signal Integration

- **No signal staleness discount** — a 5-day-old signal is treated identically to today's signal. Plan: add a score decay factor proportional to signal age.
- **No earnings date awareness** — positions hold through earnings with no volatility adjustment. Plan: reduce position size or tighten stops ahead of known earnings dates (available from FMP calendar).

### EOD Reconciliation

- **No alpha attribution by sector** — EOD email shows total alpha but not sector breakdown. Plan: compute per-sector contribution to daily alpha.

---

## Related Modules

- [`alpha-engine-research`](https://github.com/cipher813/alpha-engine-research) — Autonomous LLM research pipeline
- [`alpha-engine-predictor`](https://github.com/cipher813/alpha-engine-predictor) — GBM predictor (5-day alpha predictions)
- [`alpha-engine-backtester`](https://github.com/cipher813/alpha-engine-backtester) — Signal quality analysis and parameter optimization
- [`alpha-engine-dashboard`](https://github.com/cipher813/alpha-engine-dashboard) — Streamlit monitoring dashboard

---

## License

MIT — see [LICENSE](LICENSE).
