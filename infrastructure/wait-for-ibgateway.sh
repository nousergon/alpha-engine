#!/bin/bash
# Wait for IB Gateway to accept API connections on port 4002.
# Called by alpha-engine-morning.service before running main.py.
set -euo pipefail

PORT="${1:-4002}"
MAX_WAIT=120

for i in $(seq 1 $MAX_WAIT); do
    if (echo > /dev/tcp/127.0.0.1/"$PORT") 2>/dev/null; then
        echo "IB Gateway ready on port $PORT after ${i}s"
        exit 0
    fi
    sleep 1
done

echo "ERROR: IB Gateway not ready on port $PORT after ${MAX_WAIT}s"
exit 1
