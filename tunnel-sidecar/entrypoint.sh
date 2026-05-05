#!/bin/bash
# v3 sidecar — wait for the greffer's controller to write client.toml
# (manager pushes it via POST /api/controller/tunnel-config/ + the
# cert-poll response on accept; see greffer's app/routers/controller.py
# and app/workers/register.py). Once the file exists, exec rathole;
# its built-in file watcher takes over for subsequent updates.
#
# rathole 0.5.0 rejects an empty/missing config at parse time
# (exit 1, no retry), so we pre-flight existence to avoid a
# crash-loop until the greffer is registered + accepted.
#
# Once rathole is running, this script is done; signal forwarding is
# dumb-init's job.
set -euo pipefail

CONFIG="${RATHOLE_CLIENT_CONFIG_PATH:-/config/client.toml}"
POLL_SECONDS="${RATHOLE_BOOTSTRAP_POLL_SECONDS:-1}"

echo "tunnel-sidecar: waiting for client.toml at $CONFIG"

while true; do
    if [ -s "$CONFIG" ] && grep -q '^\[client\]' "$CONFIG" 2>/dev/null; then
        echo "tunnel-sidecar: config has [client] block, starting rathole"
        exec rathole --client "$CONFIG"
    fi
    sleep "$POLL_SECONDS"
done
