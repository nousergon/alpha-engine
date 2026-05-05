# alpha-engine — Executor Module

> System architecture, S3 layout, and cross-repo conventions: see [`~/Development/CLAUDE.md`](../CLAUDE.md). This file covers executor-specific operational details only.

## What this repo is

Execution module only. Reads AI-generated `signals.json` + `predictions.json` from S3, enforces risk rules, sizes positions, and writes the intraday order book. The daemon is the sole order executor — it uses technical triggers to time entries and executes exits immediately. No orders are placed by `main.py`.

## Stack

- Python 3.11, venv at `.venv/`
- IB Gateway (paper) on `127.0.0.1:4002` via `ib_insync`
- IB Gateway runs via Docker (gnzsnz/ib-gateway-docker) with TOTP-based 2FA on the trading instance; locally via IBC + Xvfb when needed
- SQLite `trades.db` — gitignored, backed up to S3 after each run
- Deployed on trading EC2 (t3.small, market hours only, started/stopped by the weekday + EOD Step Functions)

## Key files

```
executor/main.py                   # morning order-book planner (no orders placed)
executor/signal_reader.py          # reads signals/{date}/signals.json from S3 (falls back to prior trading day)
executor/risk_guard.py             # hard rule enforcement + graduated drawdown response
executor/position_sizer.py         # equal-weight base sizing with sector/conviction/upside/drawdown adjustments
executor/ibkr.py                   # ib_insync wrapper: NAV, positions, prices, orders
executor/trade_logger.py           # SQLite schema + S3 backup + entry_date lookup
executor/price_cache.py            # loads OHLCV from predictor S3 slim cache for ATR
executor/eod_reconcile.py          # P&L vs SPY, fires EOD email
executor/eod_emailer.py            # HTML/plain email builder
executor/connection_test.py        # quick IB Gateway connection check
executor/daemon.py                 # sole order executor — urgent exits + technical entry triggers
executor/bracket_orders.py         # BUY + trailing stop as parent/child IB orders
executor/order_book.py             # JSON-based intraday order book (entries, urgent exits, stops)
executor/price_monitor.py          # 15-min delayed streaming subscriptions
executor/intraday_exit_manager.py  # intraday exit rules (trail, profit-take, collapse)
executor/entry_triggers.py         # intraday entry triggers (pullback, VWAP, support, expiry)
executor/notifier.py               # Telegram push notifications for trades
executor/strategies/               # strategy layer — backtestable entry/exit rules
  config.py                        # strategy defaults + YAML override loader
  exit_manager.py                  # ATR trailing stops + time-based exit decay
config/risk.yaml                   # GITIGNORED — local config with real values (never commit)
config/risk.yaml.example           # template — safe to commit
infrastructure/setup-trading-ec2.sh # trading-instance bootstrap (Docker, venv, systemd, logs)
infrastructure/iam/                # codified executor-role inline policies (PR #105, 2026-04-27)
```

## Repo-specific gitignore

- `config/risk.yaml` — real config with S3 bucket names, EC2 db paths, email addresses; never committed
- `trades.db` — local SQLite mirror, S3 is canonical

## Config

`config/risk.yaml` is gitignored and never committed. It contains S3 bucket names, email addresses, and the EC2 db path. Copy `config/risk.yaml.example` to `config/risk.yaml` to set up a new environment.

## Repo-internal architecture

### Morning planner vs daemon split
- `main.py` (morning planner) reads signals + predictions, applies risk guard + position sizing, writes the order book to S3. **Places no orders.**
- `daemon.py` is the sole order executor. Urgent exits fire immediately at market open; entries fire on technical triggers (pullback, VWAP discount, support bounce, 3:55 PM ET time expiry).

### Signal reader fallback
`signal_reader.py` reads `signals/{date}/signals.json` from S3 and falls back to the prior trading day if today's file is missing. The morning planner can therefore run even if the Saturday research SF was delayed.

### EOD reconciliation flow
EOD is triggered by daemon shutdown (the `daemon.py` finally block) after market close, gated by:
1. `market_opened`: the daemon entered the live trading window
2. `not is_market_hours()`: market is closed RIGHT NOW

This invokes the `alpha-engine-eod-pipeline` Step Function: `PostMarketData` → `EODReconcile` → `StopTradingInstance`. Pre-market exits (crash, signal) and SIGTERM-driven mid-session restarts both correctly skip the trigger. There is no EventBridge cron or systemd timer fallback — the daemon is the single authoritative trigger; if it never starts, the weekday SF's `RunDaemon` step fails loud and SNS alarms fire (no-backstop design).

### Paper-account hard-exit safety check
The daemon hard-exits at startup if connected to a non-paper IB account (account ID must start with `D`). Protects against ever pointing at a live brokerage account.

### IB Gateway specifics
- Port `4002` is the paper trading port (live would be `4001`)
- IBC config: `ExistingSessionDetectedAction=primary` (NOT `primaryoverride` — that caused scenario 6 mid-session exits)
- TOTP secret stored in AWS Secrets Manager (`alpha-engine/ib-gateway-totp`)
- IB paper data is 15-min delayed → daemon trading window stops 1:15 PM PT, EOD at 1:20 PM PT

### Strategy layer
`executor/strategies/` adds backtestable quantitative logic between signal ingestion and order placement, no LLM calls:
- **ATR trailing stop** — exits when price drops below configurable ATR-based trailing stop. `strategy.exit_manager.atr_*` in risk.yaml.
- **Time-based exit decay** — reduces positions held beyond configurable thresholds, only when Research signal is HOLD. `strategy.exit_manager.time_decay_*` in risk.yaml.
- **Graduated drawdown response** — position sizes scale down through configurable tiers as drawdown deepens; hard halt is the final tier. `strategy.graduated_drawdown.*` in risk.yaml.

## Risk rules (config/risk.yaml)

All risk parameters are configurable in `config/risk.yaml`. The backtester auto-tunes safe-to-tune params via `config/executor_params.json` in S3.

Key risk dimensions:
- Max position size (% NAV, adjustable by market regime)
- Max sector exposure (% NAV)
- Max total equity (% NAV)
- Graduated drawdown response (tiered sizing reduction + circuit breaker halt)
- Min score to enter
- Declining conviction reduces position size (0.7x) but does not block entries

EXIT and REDUCE signals bypass all risk rules — reducing exposure always passes.

## Signals format consumed

`signals/{YYYY-MM-DD}/signals.json` from the research S3 bucket. Per-stock fields read by the executor:

```
signal: "ENTER" | "EXIT" | "REDUCE" | "HOLD"
score: float (0–100, must be >= min_score_to_enter to enter)
conviction: "rising" | "stable" | "declining"
price_target_upside: float (e.g. 0.18 = 18% upside)
sector_rating: "overweight" | "market_weight" | "underweight"
```

## Testing the configuration

Run in order after any deployment or EC2 change:

```bash
# 1. Confirm local config exists (gitignored — must be present or executor will fail)
ls config/risk.yaml

# 2. Test IB Gateway connection (must return Connected: True)
python executor/connection_test.py

# 3. Test S3 signal read for a known past date (replace date as needed)
python -c "import yaml; from executor.signal_reader import read_signals; c=yaml.safe_load(open('config/risk.yaml')); d=read_signals(c['signals_bucket'],'2026-03-05'); print('OK:', d['market_regime'], len(d['universe']), 'stocks')"

# 4. Full dry run — exercises signal read, pricing, sizing, risk guard; no orders placed
python executor/main.py --dry-run

# 5. On EC2: confirm git state is clean after a force push
ae-trading "cd ~/alpha-engine && git fetch origin && git reset --hard origin/main && ls config/risk.yaml"

# 6. On EC2: test IB Gateway connection
ae-trading "cd ~/alpha-engine && source .venv/bin/activate && python executor/connection_test.py"

# 7. On EC2: full dry run
ae-trading "cd ~/alpha-engine && source .venv/bin/activate && python executor/main.py --dry-run"
```

## Common commands

```bash
# Activate venv
source .venv/bin/activate

# Test IB Gateway connection
python executor/connection_test.py

# Dry run — full loop without placing orders
python executor/main.py --dry-run

# Live run (paper trading)
python executor/main.py

# EOD reconciliation
python executor/eod_reconcile.py

# SSH to trading instance (looks up current IP automatically)
ae-trading "cmd"

# Update EC2 after a force push (rewrites history)
ae-trading "cd ~/alpha-engine && git fetch origin && git reset --hard origin/main"

# Normal deploy to EC2
git push origin main && ae-trading "cd ~/alpha-engine && git pull"

# View executor / EOD logs on EC2
ae-trading "tail -50 /var/log/executor.log"
ae-trading "tail -50 /var/log/eod.log"

# Check IB Gateway service status on EC2
ae-trading "sudo systemctl status ibgateway"
```

## Trading instance boot sequence (systemd)

```
boot-pull.service → ibgateway.service
```

Only `boot-pull` (`git pull`) and IB Gateway run automatically on boot. The morning planner and daemon are NOT autostarted — they run exclusively from the weekday Step Function's `RunMorningPlanner` and `RunDaemon` SSM steps.

The prior systemd autostart units (`alpha-engine-morning.service`, `alpha-engine-daemon.service`, `alpha-engine-daemon.timer`) were redundant paths that raced the SF and ran the planner against stale ArcticDB rows before `MorningEnrich` completed (incident: 2026-05-05). Disabled 2026-05-05 via `systemctl disable` — units still exist for break-glass manual invocation but no longer auto-start. SF is the single authoritative path.

## EC2 access

Two instances with dynamic IPs. Shell helpers in `~/.zshrc`:

```bash
ae-ip               # look up both instance IPs
ae-trading "cmd"    # SSH to trading instance
ae-dashboard "cmd"  # SSH to micro (dashboard) instance
```

Micro instance ID: `i-09b539c844515d549` (us-east-1)
Trading instance ID: set after launch (see migration plan)

GitHub access on EC2: HTTPS + fine-grained PAT in `~/.netrc` (chmod 600), `Contents: read` scope per repo. First-time setup populates `~/.netrc`; thereafter `git pull` works without re-entering credentials.

## Deployment notes

- IB Gateway runs via Docker (gnzsnz/ib-gateway-docker) with TOTP-based 2FA
- TOTP secret in AWS Secrets Manager: `alpha-engine/ib-gateway-totp`
- Port `4002` = paper, `4001` = live (never use)
- Morning planner + daemon run from the weekday Step Function via SSM (NOT boot-triggered systemd, since 2026-05-05). IB Gateway + boot-pull are the only services that auto-start on boot.
- `config/risk.yaml` is the live config — gitignored, never overwritten by `git pull`
- Setup script: `infrastructure/setup-trading-ec2.sh` (Docker, venv, systemd, logs)
- Executor IAM codified under `infrastructure/iam/` (PR #105, 2026-04-27); `apply.sh` restores all 9 inline policies from source. Trust + role + managed policies still manual.

## Future opportunities

- **Real-time data feed (polygon.io WebSocket):** Replace 15-minute delayed IB data in `executor/price_monitor.py` with polygon.io real-time WebSocket feed. Improves intraday entry trigger precision (pullback/VWAP/support). Requires polygon.io paid plan. The data module (`alpha-engine-data`) already has a polygon client for grouped-daily — WebSocket would be a new integration here. Only pursue if execution quality monitoring (`trades/execution_quality/{date}.json`) shows significant slippage attributable to delayed pricing.
