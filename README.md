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

## Related Modules

- [`alpha-engine-research`](https://github.com/cipher813/alpha-engine-research) — Autonomous LLM research pipeline
- [`alpha-engine-predictor`](https://github.com/cipher813/alpha-engine-predictor) — GBM predictor (5-day alpha predictions)
- [`alpha-engine-backtester`](https://github.com/cipher813/alpha-engine-backtester) — Signal quality analysis and parameter optimization
- [`alpha-engine-dashboard`](https://github.com/cipher813/alpha-engine-dashboard) — Streamlit monitoring dashboard

---

## License

MIT — see [LICENSE](LICENSE).
