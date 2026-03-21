#!/bin/bash
# Register cron jobs for the micro (dashboard) instance.
#
# The micro instance runs 24/7 and handles:
#   1. Starting the trading instance before market open (6:15 AM PT)
#   2. Stopping the trading instance after EOD (1:30 PM PT)
#   3. Launching the backtester spot instance (Monday 08:00 UTC)
#
# All trading (executor, daemon, EOD) runs on the trading instance via
# systemd services triggered on boot. No executor crons here.
#
# Secrets file (~/.alpha-engine.env, chmod 600):
#   GMAIL_APP_PASSWORD=xxx
#   ANTHROPIC_API_KEY=yyy
#   TELEGRAM_BOT_TOKEN=xxx
#   TELEGRAM_CHAT_ID=xxx
#
# Usage:
#   TRADING_INSTANCE_ID=i-xxx bash infrastructure/add-cron.sh

set -euo pipefail

ENV_FILE="/home/ec2-user/.alpha-engine.env"

if [ -z "${TRADING_INSTANCE_ID:-}" ]; then
    echo "ERROR: TRADING_INSTANCE_ID must be set"
    echo "Usage: TRADING_INSTANCE_ID=i-xxx bash infrastructure/add-cron.sh"
    exit 1
fi

# ── Validate env file exists ─────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: ${ENV_FILE} not found."
    exit 1
fi

# ── Build cron lines ─────────────────────────────────────────────────────────
SOURCE_ENV=". ${ENV_FILE} &&"
REGION="us-east-1"

CRON_TZ_LINE="CRON_TZ=America/Los_Angeles"

# Start trading instance 15 min before market open
START_CRON="15 6 * * 1-5  aws ec2 start-instances --instance-ids ${TRADING_INSTANCE_ID} --region ${REGION} >> /var/log/trading-lifecycle.log 2>&1"

# Stop trading instance 25 min after EOD reconciliation
STOP_CRON="30 13 * * 1-5  aws ec2 stop-instances --instance-ids ${TRADING_INSTANCE_ID} --region ${REGION} >> /var/log/trading-lifecycle.log 2>&1"

# Backtester spot launch (Monday 08:00 UTC — fixed UTC, not PT)
BACKTESTER_CRON="0 8 * * 1  cd /home/ec2-user/alpha-engine-backtester && git pull --ff-only >> /var/log/backtester.log 2>&1 && ${SOURCE_ENV} bash infrastructure/spot_backtest.sh >> /var/log/backtester.log 2>&1"

# ── Replace existing entries ─────────────────────────────────────────────────
EXISTING=$(crontab -l 2>/dev/null || true)
# Remove old executor/daemon/eod/lifecycle/backtester lines
FILTERED=$(echo "$EXISTING" | grep -v "alpha-engine/.*executor/main.py" \
    | grep -v "alpha-engine/.*executor/eod_reconcile.py" \
    | grep -v "alpha-engine/.*executor.daemon" \
    | grep -v "ec2 start-instances" \
    | grep -v "ec2 stop-instances" \
    | grep -v "spot_backtest.sh" \
    | grep -v "^CRON_TZ=America/Los_Angeles$" || true)

{
    echo "$FILTERED"
    echo "$CRON_TZ_LINE"
    echo "$START_CRON"
    echo "$STOP_CRON"
    echo "$BACKTESTER_CRON"
} | crontab -

echo "Micro instance cron jobs registered:"
echo "  Trading start:  weekdays 6:15 AM PT"
echo "  Trading stop:   weekdays 1:30 PM PT"
echo "  Backtester:     Mondays 08:00 UTC (spot instance)"
echo "  Trading ID:     ${TRADING_INSTANCE_ID}"
echo ""
echo "Current crontab:"
crontab -l
