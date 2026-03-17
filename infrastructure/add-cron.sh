#!/bin/bash
# Register the executor + EOD cron jobs.
# Safe to run multiple times — replaces existing entries.
#
# Schedule (all times UTC, weekdays only):
#   13:30  executor/main.py       — morning trading loop
#   21:05  executor/eod_reconcile.py — EOD P&L + rationale email
#
# Both cron lines:
#   1. git pull --ff-only (auto-deploy latest code)
#   2. Run the Python script with required env vars
#
# Required env vars (passed inline to avoid cron env issues):
#   GMAIL_APP_PASSWORD  — Gmail app password for email delivery
#   ANTHROPIC_API_KEY   — Anthropic API key for LLM rationale synthesis (EOD)
#
# Usage:
#   GMAIL_APP_PASSWORD=xxx ANTHROPIC_API_KEY=yyy bash infrastructure/add-cron.sh
#
# Or if already set in shell:
#   bash infrastructure/add-cron.sh

set -euo pipefail

REPO_DIR="/home/ec2-user/alpha-engine"

# ── Resolve credentials ──────────────────────────────────────────────────────
GMAIL_PW="${GMAIL_APP_PASSWORD:-}"
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"

if [ -z "$GMAIL_PW" ]; then
    echo "ERROR: GMAIL_APP_PASSWORD not set. Pass it as env var or export it first."
    exit 1
fi

if [ -z "$ANTHROPIC_KEY" ]; then
    echo "WARNING: ANTHROPIC_API_KEY not set. EOD rationale will use template fallback."
fi

# ── Build cron lines ─────────────────────────────────────────────────────────
ENV_VARS="GMAIL_APP_PASSWORD=${GMAIL_PW}"
if [ -n "$ANTHROPIC_KEY" ]; then
    ENV_VARS="${ENV_VARS} ANTHROPIC_API_KEY=${ANTHROPIC_KEY}"
fi

EXECUTOR_CRON="30 13 * * 1-5  cd ${REPO_DIR} && git pull --ff-only >> /var/log/executor.log 2>&1 && ${ENV_VARS} .venv/bin/python executor/main.py >> /var/log/executor.log 2>&1"
EOD_CRON="5 21 * * 1-5  cd ${REPO_DIR} && git pull --ff-only >> /var/log/eod.log 2>&1 && ${ENV_VARS} .venv/bin/python executor/eod_reconcile.py >> /var/log/eod.log 2>&1"

# ── Replace existing entries ─────────────────────────────────────────────────
# Remove any existing alpha-engine executor/eod lines, then add new ones.
# Preserve backtester and other cron entries.
EXISTING=$(crontab -l 2>/dev/null || true)
FILTERED=$(echo "$EXISTING" | grep -v "alpha-engine/.*executor/main.py" | grep -v "alpha-engine/.*executor/eod_reconcile.py" || true)

{
    echo "$FILTERED"
    echo "$EXECUTOR_CRON"
    echo "$EOD_CRON"
} | crontab -

echo "Executor cron jobs registered:"
echo "  Executor: weekdays 13:30 UTC (6:30 AM PT)"
echo "  EOD:      weekdays 21:05 UTC (4:05 PM ET)"
echo ""
echo "Current crontab:"
crontab -l
