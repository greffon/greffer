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

# Enumerate every L4 socket currently in a "bound" state on this network
# namespace, broken out by protocol so the per-service check below can
# match TCP services against TCP listeners and UDP services against UDP
# listeners. TCP and UDP can legitimately coexist on the same port
# number; checking against a single combined snapshot would either:
#   - falsely flag a TCP-listener-vs-UDP-service combination as a
#     collision (Codex P2 on greffer#27), or
#   - miss a real UDP-vs-UDP collision because the snapshot only saw
#     TCP listeners.
#
# Implementation notes:
#   - /proc/net/{tcp,tcp6}: field 4 is the connection state in hex.
#     "0A" = LISTEN; ESTABLISHED / TIME_WAIT / etc. don't conflict
#     with rathole's bind. We filter strictly for LISTEN.
#   - /proc/net/{udp,udp6}: UDP has no LISTEN state. Any entry in
#     these files indicates a bound UDP socket on the local port —
#     that conflicts with rathole's UDP bind. No state filter applied.
#   - Field 2 is "IP_HEX:PORT_HEX"; we discard the IP because manager
#     renders 0.0.0.0 binds and any same-port listener (on any local
#     IP) conflicts with a wildcard bind via EADDRINUSE.
#   - Pure bash + awk (mawk-compatible). No iproute2 / netcat / python
#     dependency in the rathole-server image.
get_listening_ports_proto() {
    local proto=$1   # "tcp" or "udp"
    local proc4="/proc/net/${proto}"
    local proc6="/proc/net/${proto}6"
    {
        for proc in "$proc4" "$proc6"; do
            [ -r "$proc" ] || continue
            if [ "$proto" = "tcp" ]; then
                # LISTEN sockets only.
                tail -n +2 "$proc" | awk '$4 == "0A" {print $2}'
            else
                # UDP — every entry is a bound socket.
                tail -n +2 "$proc" | awk '{print $2}'
            fi
        done
    } | while IFS= read -r addr; do
        port_hex="${addr##*:}"
        # Skip rare malformed lines that don't fit the format.
        [[ "$port_hex" =~ ^[0-9A-Fa-f]+$ ]] || continue
        printf "%d\n" "0x$port_hex"
    done | sort -un
}

while true; do
    if [ -f "$CONFIG" ] && grep -q '^\[server\.services\.' "$CONFIG" 2>/dev/null; then
        echo "rathole-server: config has services, running pre-flight port check"

        # Walk the config as a state machine: track the current section's
        # ``type`` (tcp by default; rathole spec) and ``bind_addr``, then
        # flush a (proto, host:port) tuple per section. Both keys can
        # appear in any order within a section — manager renders
        # bind_addr first, but hand-edited configs are free-form. The
        # top-level ``[server]`` block has only a bind_addr (the rathole
        # control port, always TCP) — no ``type`` line, so proto stays
        # at the "tcp" default.
        #
        # Tolerates:
        #   - Both TOML string forms: "..." (basic) and '...' (literal).
        #     Manager renders double quotes, but a hand-edited or
        #     future-formatter-touched config may use either.
        #   - Arbitrary whitespace around ``=``.
        #   - A trailing ``# ...`` line comment.
        # Emits one tab-separated ``proto<TAB>host:port`` per service.
        service_binds=$(awk '
          /^[[:space:]]*\[/ {
            if (bind != "") print proto "\t" bind
            proto = "tcp"
            bind = ""
            next
          }
          /^[[:space:]]*type[[:space:]]*=/ {
            line = $0
            sub(/^[^=]*=[[:space:]]*/, "", line)
            sub(/[[:space:]]*(#.*)?$/, "", line)
            gsub(/^["\x27]|["\x27]$/, "", line)
            proto = line
            next
          }
          /^[[:space:]]*bind_addr[[:space:]]*=/ {
            line = $0
            sub(/^[^=]*=[[:space:]]*/, "", line)
            sub(/[[:space:]]*(#.*)?$/, "", line)
            gsub(/^["\x27]|["\x27]$/, "", line)
            bind = line
          }
          END {
            if (bind != "") print proto "\t" bind
          }
        ' "$CONFIG")

        # Snapshot of currently-bound ports per protocol — read once
        # instead of per-port to keep the check O(N + M).
        tcp_ports=$(get_listening_ports_proto tcp)
        udp_ports=$(get_listening_ports_proto udp)

        collision_count=0
        while IFS=$'\t' read -r proto pair; do
            [ -z "$pair" ] && continue
            port="${pair##*:}"
            host="${pair%:*}"
            [[ "$port" =~ ^[0-9]+$ ]] || continue

            # Pick the snapshot matching this service's protocol.
            # Default ("tcp" or any unrecognized value) → tcp_ports.
            if [ "$proto" = "udp" ]; then
                snapshot="$udp_ports"
            else
                snapshot="$tcp_ports"
            fi

            # Port-only match against the protocol-specific snapshot.
            # Same port + same protocol = real EADDRINUSE waiting to
            # happen at rathole's bind; same port + different protocol
            # = no conflict (TCP/UDP coexist).
            if grep -qx "$port" <<< "$snapshot"; then
                echo "rathole-server: WARNING — pre-flight ${proto} port collision: ${host}:${port} is already bound on this host (some local interface is listening)." >&2
                echo "rathole-server: WARNING — rathole will fail to bind this ${proto} port and the corresponding service will not carry traffic." >&2
                echo "rathole-server: WARNING — fix: stop the conflicting process, OR shift TUNNEL_PORT_RANGE to an unused window, OR reserve the range via /proc/sys/net/ipv4/ip_local_reserved_ports." >&2
                collision_count=$((collision_count + 1))
            fi
        done <<< "$service_binds"

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
