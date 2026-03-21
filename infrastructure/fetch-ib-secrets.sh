#!/bin/bash
# Fetch IB Gateway credentials from AWS Secrets Manager and start Docker container.
# Called by ibgateway.service on boot.
set -euo pipefail

COMPOSE_FILE="/home/ec2-user/alpha-engine/infrastructure/docker-compose.yml"
ENV_FILE="/home/ec2-user/.alpha-engine.env"
SECRET_ID="alpha-engine/ib-gateway-totp"
REGION="us-east-1"

# Source env file for IB_USER and IB_PASS
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

if [ -z "${IB_USER:-}" ] || [ -z "${IB_PASS:-}" ]; then
    echo "ERROR: IB_USER and IB_PASS must be set in $ENV_FILE"
    exit 1
fi

# Fetch TOTP secret from Secrets Manager
TOTP_SECRET=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_ID" \
    --region "$REGION" \
    --query SecretString --output text \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['TOTP_SECRET'])")

if [ -z "$TOTP_SECRET" ]; then
    echo "ERROR: Could not fetch TOTP secret from Secrets Manager"
    exit 1
fi

export IB_USER IB_PASS TOTP_SECRET

echo "Starting IB Gateway Docker container..."
docker compose -f "$COMPOSE_FILE" up -d

# Wait for IB Gateway to be ready (poll port 4002)
echo "Waiting for IB Gateway to accept connections on port 4002..."
for i in $(seq 1 60); do
    if nc -z 127.0.0.1 4002 2>/dev/null; then
        echo "IB Gateway ready after ${i}s"
        exit 0
    fi
    sleep 1
done

echo "WARNING: IB Gateway did not become ready within 60s — proceeding anyway"
