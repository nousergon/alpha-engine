#!/bin/bash
# Full setup for the trading EC2 instance (t3.small, market hours only).
#
# This instance runs IB Gateway (Docker), the morning batch (main.py),
# the intraday daemon, and EOD reconciliation. It is started/stopped
# daily by the micro instance's cron.
#
# Prerequisites:
#   - Amazon Linux 2023 AMI
#   - ~/.netrc with GitHub PAT (for git pull)
#   - ~/.alpha-engine.env with secrets (IB_USER, IB_PASS, GMAIL_APP_PASSWORD,
#     ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
#   - TOTP secret stored in AWS Secrets Manager (alpha-engine/ib-gateway-totp)
#   - config/risk.yaml created manually (gitignored)
#
# Usage:
#   bash ~/alpha-engine/infrastructure/setup-trading-ec2.sh

set -euo pipefail

REPO_DIR="/home/ec2-user/alpha-engine"
ENV_FILE="/home/ec2-user/.alpha-engine.env"

echo "=== Alpha Engine Trading Instance Setup ==="

# ── 1. Docker ────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    sudo dnf install -y docker
    sudo systemctl enable docker
    sudo systemctl start docker
    sudo usermod -aG docker ec2-user
    echo "Docker installed. You may need to log out and back in for group changes."
fi

# Install docker compose plugin if not present
if ! docker compose version &>/dev/null; then
    echo "Installing Docker Compose plugin..."
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | head -1 | sed 's/.*"v\(.*\)".*/\1/')
    sudo curl -SL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# ── 2. Python venv ───────────────────────────────────────────────────────────
cd "$REPO_DIR"
if [ ! -d ".venv" ]; then
    echo "Creating virtualenv..."
    python3.11 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# ── 3. Log files ─────────────────────────────────────────────────────────────
for log in executor.log eod.log daemon.log; do
    sudo touch "/var/log/$log"
    sudo chown ec2-user:ec2-user "/var/log/$log"
done
echo "Log files ready"

# ── 4. Config check ─────────────────────────────────────────────────────────
if [ ! -f config/risk.yaml ]; then
    echo ""
    echo "WARNING: config/risk.yaml not found."
    echo "  cp config/risk.yaml.example config/risk.yaml"
    echo ""
fi

if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "WARNING: $ENV_FILE not found."
    echo "  Create it with IB_USER, IB_PASS, and other secrets."
    echo ""
fi

# ── 5. Boot-pull service ────────────────────────────────────────────────────
sudo bash "$REPO_DIR/infrastructure/install-boot-pull.sh"

# ── 6. Systemd services ────────────────────────────────────────────────────
SYSTEMD_DIR="$REPO_DIR/infrastructure/systemd"

# Copy all service and timer files
for unit in ibgateway.service alpha-engine-morning.service alpha-engine-daemon.service \
            alpha-engine-eod.service alpha-engine-eod.timer; do
    sudo cp "$SYSTEMD_DIR/$unit" /etc/systemd/system/
done

sudo systemctl daemon-reload

# Enable services (they start on boot via dependencies)
sudo systemctl enable ibgateway.service
sudo systemctl enable alpha-engine-morning.service
sudo systemctl enable alpha-engine-daemon.service
sudo systemctl enable alpha-engine-eod.timer

echo ""
echo "=== Trading Instance Setup Complete ==="
echo ""
echo "Services enabled (boot-triggered):"
echo "  1. boot-pull.service     — git pull all repos"
echo "  2. ibgateway.service     — IB Gateway Docker + TOTP auth"
echo "  3. alpha-engine-morning  — order book planner (main.py)"
echo "  4. alpha-engine-daemon   — intraday order executor"
echo "  5. alpha-engine-eod      — EOD reconciliation (1:05 PM PT timer)"
echo ""
echo "Test: sudo systemctl start ibgateway && sleep 30 && python executor/connection_test.py"
echo "Dry run: python executor/main.py --dry-run"
