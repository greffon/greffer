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

# A reload needs a live MASTER, so test for exactly that -- the same pid file
# `nginx -s reload` itself signals. `pidof nginx` is not equivalent: when the
# master dies its workers are orphaned but keep running (and keep holding :443),
# so `pidof` reports "up", we take the reload branch, and nginx answers with
# `kill(<pid>, 1) failed (3: No such process)`. Nothing recovers and the orphans
# serve the stale cert forever.
master_alive() {
    [ -s /var/run/nginx.pid ] && kill -0 "$(cat /var/run/nginx.pid)" 2>/dev/null
}

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
    if master_alive; then
        echo "Reloading Nginx Configuration"
        if nginx -s reload; then
            return 0
        fi
        # SIGHUP did not land (master died between the check and the signal, or
        # the pid file is stale). Do NOT treat that as done: with *.crl excluded
        # there is no periodic event to try again on, so returning here would
        # leave orphaned workers serving the stale cert indefinitely. Fall
        # through to the restart path.
        echo "Reload failed, falling back to a restart"
    fi
    # No live master. Any surviving worker is an orphan that still holds
    # 0.0.0.0:443, so a plain start would fail with the very "Address in use"
    # this script exists to avoid -- reap the orphans first. Only reachable
    # once the master is already gone, i.e. nginx is broken either way.
    if pidof nginx >/dev/null 2>&1; then
        echo "Nginx master gone, reaping orphaned workers before restart"
        killall nginx 2>/dev/null
        # Wait for the port to actually be released. killall only sends TERM,
        # and a worker draining a request can outlive a fixed sleep -- starting
        # while it still holds :443 fails with "Address in use", and with *.crl
        # excluded there is no periodic event to retry on.
        i=0
        while pidof nginx >/dev/null 2>&1; do
            i=$((i + 1))
            if [ "$i" -ge 10 ]; then
                echo "Orphans survived TERM, sending KILL"
                killall -KILL nginx 2>/dev/null
                sleep 1
                break
            fi
            sleep 1
        done
    fi
    i=0
    while :; do
        echo "Starting Nginx"
        nginx && return 0
        i=$((i + 1))
        [ "$i" -ge 3 ] && break
        echo "Start failed, retrying in 2s"
        sleep 2
    done
    echo "Nginx failed to start after 3 attempts, retrying on the next /root change"
}

while true; do
    # Reconcile only once the watch is provably active, which is why -q is off:
    # inotifywait announces "Watches established." on stderr, we fold that into
    # the pipe and treat it as the signal. Reconciling before that (or before
    # spawning inotifywait at all) leaves a window where a cert write is neither
    # queued by the watch nor seen by the reconcile, and the write is then lost
    # for good -- excluding *.crl removed the every-5-min churn that used to
    # paper over such a miss, so a fresh container would sit down, or a
    # restarted one serve the old cert, until some unrelated later change.
    # The same reasoning covers each respawn below: re-established watch,
    # re-reconcile.
    inotifywait -m --exclude '\.crl$' -e create -e modify -e delete -e move /root/ 2>&1 |
    while read -r line; do
        case "$line" in
            "Setting up watches."*) continue ;;
            "Watches established."*)
                # Nothing can be missed from here on; pick up whatever landed
                # before the watch existed (e.g. a container restart with the
                # certs still on disk).
                start_or_reload
                continue
                ;;
        esac
        # Let the burst settle before testing. A cert install is three
        # separate Docker writes (pem.crt, then cert.key, then ca.pem), so
        # acting on the first one tests the NEW cert against the OLD key:
        # `nginx -t` fails on a key mismatch and every routine renewal logs
        # an [emerg] that reads like a broken config, then reloads three
        # times instead of once. Monitor mode queues the rest while we
        # drain, so waiting for a quiet second collapses the burst into a
        # single (re)load of a consistent pair. Timing out (or EOF) ends the
        # drain; either way we then act on everything seen so far.
        #
        # `read -t` is outside POSIX sh, but this script only ever runs as
        # the CMD of nginx:*-alpine, whose /bin/sh is busybox ash and does
        # support it (verified in the image).
        # shellcheck disable=SC3045
        while read -r -t 1 _; do :; done
        start_or_reload
    done
    # inotifywait exited (watch lost / error); back off so we can't busy-spin,
    # then respawn the monitor.
    sleep 5
done
