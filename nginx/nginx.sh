#!/bin/sh
# Watch /root for cert/config changes and (re)load nginx accordingly.
#
# The previous version "reloaded" by running bare `nginx`, which starts a
# SECOND master that cannot bind 0.0.0.0:443 ("Address in use") and exits, so
# after the first start NO change ever took effect: cert rotations were silently
# dropped until a full container restart. SIGHUP (`nginx -s reload`) keeps the
# listening sockets and re-execs workers with the new config.
#
# We also exclude *.crl: the manager's CRL is copied into /root for historical
# reasons but this nginx never references it (no ssl_crl here), so reacting to
# it just churned a reload every sync interval (incident 2026-06-13).
#
# We watch in --monitor mode so NO event is ever lost. The manager installs the
# cert as separate Docker writes (pem.crt, then cert.key, then ca.pem); a
# one-shot inotifywait exits after the first event and stops watching while
# `nginx -t` runs, so a key write landing in that window is missed and nginx
# never (re)loads until some unrelated later change. In monitor mode the kernel
# queues events while we process, so the iteration where both cert and key are
# present always converges. The outer loop respawns the watch if it ever dies.

start_or_reload() {
    # `nginx -t` fails when the cert material is not present yet (a fresh
    # container has an empty /root until the manager installs the cert), which
    # is expected. Distinguish that from a genuinely broken config so a real
    # error is not swallowed silently.
    if ! nginx -t >/dev/null 2>&1; then
        if [ -s /root/pem.crt ]; then
            echo "nginx -t failed with cert present (bad config?), not (re)loading:"
            nginx -t
        fi
        return 0
    fi
    if pidof nginx >/dev/null 2>&1; then
        echo "Reloading Nginx Configuration"
        nginx -s reload
    else
        echo "Starting Nginx"
        nginx
    fi
}

# Bring nginx up if usable config + certs are already in place (e.g. a container
# restart with the certs still on disk); otherwise the first event starts it.
start_or_reload

while true; do
    inotifywait -m -q --exclude '\.crl$' -e create -e modify -e delete -e move /root/ |
    while read -r _; do
        start_or_reload
    done
    # inotifywait exited (watch lost / error); back off so we can't busy-spin,
    # then respawn the monitor.
    sleep 5
done
