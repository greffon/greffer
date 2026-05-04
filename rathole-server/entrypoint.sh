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

        collision_count=0
        for pair in $bind_pairs; do
            # Split host:port. For 0.0.0.0:N or :N, probe localhost
            # (rathole binding to all interfaces will conflict with
            # anything bound on loopback for the same port).
            port="${pair##*:}"
            host="${pair%:*}"
            case "$host" in
                ''|'0.0.0.0'|'[::]'|'*') host=127.0.0.1 ;;
            esac
            # Use bash's /dev/tcp pseudo-device — no nc/netstat dependency
            # in the rathole image. ``timeout 1`` bounds the probe in
            # case the host is firewalled rather than refusing.
            if timeout 1 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
                # Got a connection → something already accepting on that
                # port → collision. Close the FD we just opened.
                exec 3<&-
                exec 3>&-
                echo "rathole-server: WARNING — pre-flight port collision: ${host}:${port} is already bound." >&2
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
