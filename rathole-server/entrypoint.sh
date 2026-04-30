#!/bin/bash
# Wait until the manager has rendered a server.toml with at least one
# real service block, then exec rathole. rapiz1/rathole 0.5.0 rejects
# a [server] block with no ``services`` field at parse time —
# exit code 1, no retry — so launching it before the manager has
# registered any tunnel greffer would crash-loop until the manager
# catches up. We pre-flight the file and only invoke rathole when
# it'll actually start.
#
# Once rathole is running, its built-in file watcher takes over for
# subsequent config changes (verified in the spike at greffon/greffon
# docs/features/tunnel-support/spike/). This script does NOT manage
# rathole after startup; signal forwarding is dumb-init's job.
set -euo pipefail

CONFIG="${RATHOLE_CONFIG_PATH:-/config/server.toml}"
POLL_SECONDS="${RATHOLE_BOOTSTRAP_POLL_SECONDS:-1}"

echo "rathole-server: waiting for at least one [server.services.*] block in $CONFIG"

while true; do
    if [ -f "$CONFIG" ] && grep -q '^\[server\.services\.' "$CONFIG" 2>/dev/null; then
        echo "rathole-server: config has services, starting rathole"
        exec rathole --server "$CONFIG"
    fi
    sleep "$POLL_SECONDS"
done
