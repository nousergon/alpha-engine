#!/bin/bash
# infrastructure/reset-portfolio.sh — Reset Alpha Engine portfolio to clean state.
#
# Clears all trade history, EOD data, and order book. Use after resetting
# the IB paper account balance in the IB Account Management web portal.
#
# Usage:
#   ./infrastructure/reset-portfolio.sh --dry-run    # show what would happen, change nothing
#   ./infrastructure/reset-portfolio.sh --live        # execute the full reset
#
# Prerequisites:
#   - AWS CLI configured (or IAM role on EC2)
#   - IB paper account already reset via IB web portal (manual step)
#   - Trading instance should be STOPPED (no executor/daemon running)
#
# What gets reset:
#   - S3: eod_pnl.csv, trades_full.csv, trades_latest.db → archived then replaced
#   - S3: data/order_book.json → deleted
#   - EC2: trades.db → archived then replaced with empty schema
#   - EC2: data/order_book.json → deleted
#
# What is preserved:
#   - S3: signals/, predictor/, backtest/, config/ — all untouched
#   - S3: trades/archive/ — old data moved here
#   - Code, configs, systemd services — untouched
#
# After running --live:
#   1. Start the trading instance (EventBridge will do this on next trading day)
#   2. Verify IB paper account shows $1,000,000 balance
#   3. First EOD reconcile will create the new inception row

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
TRADES_BUCKET="${TRADES_BUCKET:-alpha-engine-executor}"
SIGNALS_BUCKET="${SIGNALS_BUCKET:-alpha-engine-research}"
ARCHIVE_DATE=$(date +%Y%m%d-%H%M%S)
ARCHIVE_PREFIX="trades/archive/${ARCHIVE_DATE}"

# EC2 instance IDs (for status check)
TRADING_INSTANCE_ID="${TRADING_INSTANCE_ID:-i-018eb3307a21329bf}"

# ── Parse flags ────────────────────────────────────────────────────────────────
MODE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  MODE="dry-run"; shift ;;
        --live)     MODE="live"; shift ;;
        *)          echo "Usage: $0 --dry-run | --live"; exit 1 ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "Usage: $0 --dry-run | --live"
    echo ""
    echo "  --dry-run    Show what would happen, change nothing"
    echo "  --live       Execute the full reset (DESTRUCTIVE)"
    exit 1
fi

DRY_RUN=true
[ "$MODE" = "live" ] && DRY_RUN=false

# ── Helper ─────────────────────────────────────────────────────────────────────
run() {
    if $DRY_RUN; then
        echo "  [DRY RUN] $*"
    else
        echo "  $*"
        eval "$@"
    fi
}

echo "═══════════════════════════════════════════════════════════════"
echo "  Alpha Engine Portfolio Reset"
echo "═══════════════════════════════════════════════════════════════"
echo "  Mode          : $MODE"
echo "  Trades bucket : $TRADES_BUCKET"
echo "  Archive prefix: $ARCHIVE_PREFIX"
echo "  Date          : $(date)"
echo ""

# ── Preflight checks ──────────────────────────────────────────────────────────

# 1. Verify trading instance is stopped
echo "==> Preflight: checking trading instance state..."
INSTANCE_STATE=$(aws ec2 describe-instances \
    --instance-ids "$TRADING_INSTANCE_ID" \
    --query "Reservations[0].Instances[0].State.Name" \
    --output text --region "$AWS_REGION" 2>/dev/null || echo "unknown")

if [ "$INSTANCE_STATE" != "stopped" ] && ! $DRY_RUN; then
    echo "  ERROR: Trading instance is '$INSTANCE_STATE' — must be stopped before reset."
    echo "  Stop it first: aws ec2 stop-instances --instance-ids $TRADING_INSTANCE_ID"
    exit 1
fi
echo "  Trading instance: $INSTANCE_STATE ✓"

# 2. Check S3 access
echo "==> Preflight: checking S3 access..."
if ! aws s3 ls "s3://${TRADES_BUCKET}/trades/" --region "$AWS_REGION" > /dev/null 2>&1; then
    echo "  ERROR: Cannot access s3://${TRADES_BUCKET}/trades/"
    exit 1
fi
echo "  S3 access: OK ✓"
echo ""

# ── Step 1: Archive existing data ─────────────────────────────────────────────
echo "==> Step 1: Archiving existing data to s3://${TRADES_BUCKET}/${ARCHIVE_PREFIX}/"

for key in trades/eod_pnl.csv trades/trades_full.csv trades/trades_latest.db; do
    if aws s3 ls "s3://${TRADES_BUCKET}/${key}" --region "$AWS_REGION" > /dev/null 2>&1; then
        run "aws s3 cp s3://${TRADES_BUCKET}/${key} s3://${TRADES_BUCKET}/${ARCHIVE_PREFIX}/$(basename $key) --region $AWS_REGION --quiet"
    else
        echo "  [skip] $key not found"
    fi
done

# Archive all dated trade backups
echo "  Archiving dated backups..."
DATED_DBS=$(aws s3 ls "s3://${TRADES_BUCKET}/trades/" --region "$AWS_REGION" 2>/dev/null | grep "trades_2" | awk '{print $4}')
for db in $DATED_DBS; do
    run "aws s3 mv s3://${TRADES_BUCKET}/trades/${db} s3://${TRADES_BUCKET}/${ARCHIVE_PREFIX}/${db} --region $AWS_REGION --quiet"
done

echo ""

# ── Step 2: Create empty replacements ─────────────────────────────────────────
echo "==> Step 2: Writing clean files to S3"

# Empty eod_pnl.csv (header only)
HEADER="date,portfolio_nav,daily_return_pct,spy_return_pct,daily_alpha_pct,positions_snapshot,created_at,spy_close"
if $DRY_RUN; then
    echo "  [DRY RUN] Write header-only eod_pnl.csv"
else
    echo "$HEADER" | aws s3 cp - "s3://${TRADES_BUCKET}/trades/eod_pnl.csv" --region "$AWS_REGION" --quiet
    echo "  Wrote empty eod_pnl.csv"
fi

# Empty trades_full.csv (header only)
TRADES_HEADER="date,ticker,action,shares,price_at_order,portfolio_nav_at_order,position_pct,ib_order_id,fill_price,fill_time,filled_shares,status,research_score,research_conviction,research_rating,sector_rating,market_regime,predicted_direction,prediction_confidence,exit_reason,rationale_json,execution_latency_ms"
if $DRY_RUN; then
    echo "  [DRY RUN] Write header-only trades_full.csv"
else
    echo "$TRADES_HEADER" | aws s3 cp - "s3://${TRADES_BUCKET}/trades/trades_full.csv" --region "$AWS_REGION" --quiet
    echo "  Wrote empty trades_full.csv"
fi

# Empty trades.db (create with schema, upload)
if $DRY_RUN; then
    echo "  [DRY RUN] Create empty trades.db with schema and upload"
else
    TMP_DB=$(mktemp /tmp/trades_reset_XXXX.db)
    python3 -c "
import sqlite3
conn = sqlite3.connect('$TMP_DB')
conn.executescript('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, ticker TEXT, action TEXT, shares INTEGER,
    price_at_order REAL, portfolio_nav_at_order REAL, position_pct REAL,
    ib_order_id INTEGER, fill_price REAL, fill_time TEXT,
    filled_shares INTEGER, status TEXT,
    research_score REAL, research_conviction TEXT, research_rating TEXT,
    sector_rating TEXT, market_regime TEXT,
    predicted_direction TEXT, prediction_confidence REAL,
    price_target_upside REAL,
    exit_reason TEXT, rationale_json TEXT, execution_latency_ms REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS eod_pnl (
    date TEXT PRIMARY KEY, portfolio_nav REAL,
    daily_return_pct REAL, spy_return_pct REAL, daily_alpha_pct REAL,
    positions_snapshot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    spy_close REAL
);
''')
conn.close()
"
    aws s3 cp "$TMP_DB" "s3://${TRADES_BUCKET}/trades/trades_latest.db" --region "$AWS_REGION" --quiet
    rm -f "$TMP_DB"
    echo "  Wrote empty trades_latest.db"
fi

# Delete order book
run "aws s3 rm s3://${TRADES_BUCKET}/data/order_book.json --region $AWS_REGION 2>/dev/null || true"
echo ""

# ── Step 3: Clear health status ───────────────────────────────────────────────
echo "==> Step 3: Clearing executor health status"
run "aws s3 rm s3://${SIGNALS_BUCKET}/health/executor.json --region $AWS_REGION 2>/dev/null || true"
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
if $DRY_RUN; then
    echo "  DRY RUN COMPLETE — no changes made."
    echo ""
    echo "  To execute for real:"
    echo "    1. Reset IB paper account at https://www.interactivebrokers.com"
    echo "       (Account Management → Settings → Paper Trading Account → Reset)"
    echo "    2. Stop the trading instance:"
    echo "       aws ec2 stop-instances --instance-ids $TRADING_INSTANCE_ID"
    echo "    3. Run this script with --live:"
    echo "       $0 --live"
    echo "    4. Start trading instance (or wait for next EventBridge trigger)"
else
    echo "  RESET COMPLETE."
    echo ""
    echo "  Archived to: s3://${TRADES_BUCKET}/${ARCHIVE_PREFIX}/"
    echo ""
    echo "  Next steps:"
    echo "    1. Verify IB paper account is reset to \$1,000,000"
    echo "    2. Start the trading instance (or wait for EventBridge)"
    echo "    3. First EOD reconcile will create the new inception row"
    echo "    4. Dashboard will show new inception date automatically"
fi
echo "═══════════════════════════════════════════════════════════════"
