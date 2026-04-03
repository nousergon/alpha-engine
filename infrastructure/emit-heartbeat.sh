#!/usr/bin/env bash
# infrastructure/emit-heartbeat.sh — Emit a CloudWatch custom metric heartbeat.
#
# Called at the end of each EC2/spot process to signal successful completion.
# CloudWatch dead-man switch alarms fire when these metrics stop arriving.
#
# Usage:
#   bash emit-heartbeat.sh <process-name>
#   bash emit-heartbeat.sh executor-morning
#   bash emit-heartbeat.sh executor-daemon
#   bash emit-heartbeat.sh backtester
#   bash emit-heartbeat.sh predictor-training
#   bash emit-heartbeat.sh rag-ingestion
#
# The metric is published to the "AlphaEngine" namespace as:
#   MetricName=Heartbeat, Dimensions=[Process=<name>], Value=1

set -euo pipefail

PROCESS_NAME="${1:?Usage: emit-heartbeat.sh <process-name>}"
AWS_REGION="${AWS_REGION:-us-east-1}"
NAMESPACE="AlphaEngine"

aws cloudwatch put-metric-data \
  --namespace "$NAMESPACE" \
  --metric-name "Heartbeat" \
  --dimensions "Process=${PROCESS_NAME}" \
  --value 1 \
  --unit "Count" \
  --region "$AWS_REGION" 2>/dev/null \
  && echo "Heartbeat emitted: ${PROCESS_NAME}" \
  || echo "WARNING: Failed to emit heartbeat for ${PROCESS_NAME} (non-fatal)"
