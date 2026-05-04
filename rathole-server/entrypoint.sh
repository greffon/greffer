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
#
# v3 hardening: before exec'ing rathole, parse the bind ports from the
# config and warn if any of them is already bound on this host. Symptom
# without the check: rathole starts, tries to bind on a colliding port,
# logs the failure deep in its own output, and the operator only finds
# out when traffic doesn't flow. This complements manager's
# tunnel_port_collisions_total metric (which guards allocation time);
# this check guards startup time, when rathole-server is first coming
# up against a config that may have been written long before the
# container started.
set -euo pipefail

CONFIG="${RATHOLE_CONFIG_PATH:-/config/server.toml}"
POLL_SECONDS="${RATHOLE_BOOTSTRAP_POLL_SECONDS:-1}"

echo "rathole-server: waiting for at least one [server.services.*] block in $CONFIG"

# Enumerate every TCP port currently in LISTEN state on this network
# namespace, regardless of which local IP the listener is bound to.
#
# We must not just probe 127.0.0.1 — Codex P1 on greffer#27 caught the
# false-negative: a process bound to 127.0.0.2:N or eth0:N would still
# conflict with rathole's later 0.0.0.0:N bind (kernel: EADDRINUSE),
# but a TCP-connect to 127.0.0.1:N would refuse and report "free".
# Reading /proc/net/tcp{,6} sees ALL local listeners.
#
# Returns ports as decimal integers, one per line, deduped + sorted.
# Works against /proc inside the container's network namespace, which
# is what matters: rathole's bind happens here too.
#
# Implementation notes:
#  - /proc/net/tcp format: "  sl  local_address rem_address st ...". Field
#    4 is the connection state in hex; "0A" = LISTEN.
#  - Field 2 is "IP_HEX:PORT_HEX". The port is the last 4 hex chars
#    after the colon. We don't care about the IP for collision
#    detection — manager renders 0.0.0.0 binds, and any local listener
#    on the same port conflicts with that.
#  - Uses pure bash + awk (mawk-compatible) — no iproute2 / netcat /
#    python dependency added to the rathole-server image.
get_listening_ports() {
    {
        for proc in /proc/net/tcp /proc/net/tcp6; do
            [ -r "$proc" ] || continue
            tail -n +2 "$proc" | awk '$4 == "0A" {print $2}'
        done
    } | while IFS= read -r addr; do
        # addr is "IP_HEX:PORT_HEX"; take the part after the last colon,
        # convert from hex to decimal via bash arithmetic.
        port_hex="${addr##*:}"
        # Skip rare malformed lines that don't fit the format.
        [[ "$port_hex" =~ ^[0-9A-Fa-f]+$ ]] || continue
        printf "%d\n" "0x$port_hex"
    done | sort -un
}

while true; do
    if [ -f "$CONFIG" ] && grep -q '^\[server\.services\.' "$CONFIG" 2>/dev/null; then
        echo "rathole-server: config has services, running pre-flight port check"

        # Extract ``bind_addr = "..."`` values from the config — both
        # the top-level ``[server]`` block (control port) and each
        # ``[server.services.*]`` block (data ports). Tolerates mixed
        # quoting and arbitrary whitespace around the ``=``. Emits one
        # ``host:port`` per matched line.
        bind_pairs=$(grep -oE 'bind_addr[[:space:]]*=[[:space:]]*"[^"]+"' "$CONFIG" \
            | sed -E 's/bind_addr[[:space:]]*=[[:space:]]*"([^"]+)"/\1/' \
            || true)

        # Snapshot of currently-listening ports — read once instead of
        # per-port to keep the check O(N + M) rather than O(N·M).
        listening_ports=$(get_listening_ports)

        collision_count=0
        for pair in $bind_pairs; do
            port="${pair##*:}"
            host="${pair%:*}"
            # Sanity-check the port parsed cleanly.
            [[ "$port" =~ ^[0-9]+$ ]] || continue

            # If ANY local listener is on this port, rathole's later
            # 0.0.0.0:port bind will fail with EADDRINUSE — regardless
            # of whether the existing listener is on loopback, eth0,
            # or any other interface. Port-only check is the right
            # granularity given manager always renders 0.0.0.0 binds.
            if grep -qx "$port" <<< "$listening_ports"; then
                echo "rathole-server: WARNING — pre-flight port collision: ${host}:${port} is already bound on this host (some local interface is listening)." >&2
                echo "rathole-server: WARNING — rathole will fail to bind this port and the corresponding service will not carry traffic." >&2
                echo "rathole-server: WARNING — fix: stop the conflicting process, OR shift TUNNEL_PORT_RANGE to an unused window, OR reserve the range via /proc/sys/net/ipv4/ip_local_reserved_ports." >&2
                collision_count=$((collision_count + 1))
            fi
        done

        if [ "$collision_count" -gt 0 ]; then
            echo "rathole-server: ${collision_count} port collision(s) detected; rathole starting anyway (it will report bind failures in its own log)" >&2
        else
            echo "rathole-server: pre-flight clean — no port collisions detected"
        fi

        echo "rathole-server: starting rathole"
        exec rathole --server "$CONFIG"
    fi
    sleep "$POLL_SECONDS"
done
