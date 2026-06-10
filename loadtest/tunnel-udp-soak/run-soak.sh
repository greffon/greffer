#!/usr/bin/env bash
# Run the tunnel-mode UDP soak: bring up the rig, drive the proxy-mode baseline
# and the relay, apply the acceptance bar, tear down. The exit code is the bar
# result (0 pass, 1 fail), so this drops into a load-test / CI step.
#
# Run on a Linux Docker Engine host. Tune via env: FLOWS, RATE (per-flow pps),
# DURATION (s), MAX_LOSS_PCT, MAX_P99_ADDED_MS. The defaults are a starting
# point; set the real bar from the first run (per the design note).
set -euo pipefail
cd "$(dirname "$0")"

FLOWS="${FLOWS:-20}"
RATE="${RATE:-200}"
DURATION="${DURATION:-60}"
MAX_LOSS_PCT="${MAX_LOSS_PCT:-2.0}"
MAX_P99_ADDED_MS="${MAX_P99_ADDED_MS:-50.0}"
SETTLE="${SETTLE:-8}"

compose() { docker compose -f docker-compose.soak.yml "$@"; }
cleanup() { compose down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "soak: bringing up rig (rathole-server + rathole-client + echo)"
compose up -d
echo "soak: waiting ${SETTLE}s for the rathole control + data channel to establish"
sleep "$SETTLE"

echo "soak: baseline (direct UDP, no relay) -> 127.0.0.1:7777"
python3 udp_soak.py --target 127.0.0.1:7777 \
  --flows "$FLOWS" --rate "$RATE" --duration "$DURATION" | tee baseline.json

echo "soak: relay (through rathole) -> 127.0.0.1:10000"
python3 udp_soak.py --target 127.0.0.1:10000 \
  --flows "$FLOWS" --rate "$RATE" --duration "$DURATION" | tee relay.json

echo "soak: applying acceptance bar (loss <= ${MAX_LOSS_PCT}%, p99 add <= ${MAX_P99_ADDED_MS}ms)"
python3 compare_soak.py --relay relay.json --baseline baseline.json \
  --max-loss-pct "$MAX_LOSS_PCT" --max-p99-added-ms "$MAX_P99_ADDED_MS"
