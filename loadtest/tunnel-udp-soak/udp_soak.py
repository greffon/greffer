#!/usr/bin/env python3
"""UDP soak / load driver for the tunnel-mode L4 acceptance bar.

Drives N concurrent UDP flows at a fixed per-flow rate for a duration against a
UDP echo endpoint, measuring per-datagram round-trip latency and loss. Used to
set / confirm the rathole UDP relay acceptance bar (latency and loss for
tolerant UDP): run it once through the relay and once against a direct
(proxy-mode) baseline, then compare with ``compare_soak.py``.

Each datagram carries an 8-byte big-endian sequence number; the echo reflects it
back unchanged. A datagram is "received" if its sequence comes back before the
drain ends; anything else is loss. Latency is the send-to-echo round trip.

Pure stdlib (sockets + threading), Python 3.8+. Output is a JSON metrics object
on stdout.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

_SEQ = struct.Struct('!Q')

# Each flow's wire sequence numbers live in a disjoint band (idx * _FLOW_STRIDE
# + local), so they are globally unique across flows. If the relay ever
# cross-routes a reply to the wrong flow's socket, the wire seq will not be in
# that flow's `inflight`, so it is ignored (and the originating flow correctly
# counts the datagram as lost) instead of being false-matched. _FLOW_STRIDE is
# far larger than any realistic per-flow datagram count (rate * duration).
_FLOW_STRIDE = 1_000_000_000


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _run_flow(host, port, rate, duration, payload_size, drain, out, idx):
    """One flow: send at ``rate`` datagrams/s for ``duration`` s, receive echoes
    concurrently, then drain for ``drain`` s to catch late ones."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.25)
    inflight = {}            # seq -> send monotonic time
    rtts = []
    lock = threading.Lock()
    stop = threading.Event()

    def receiver():
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 8:
                continue
            seq = _SEQ.unpack(data[:8])[0]
            now = time.monotonic()
            with lock:
                sent_at = inflight.pop(seq, None)
            if sent_at is not None:
                rtts.append(now - sent_at)

    rx = threading.Thread(target=receiver, daemon=True)
    rx.start()

    pad = b'x' * max(0, payload_size - 8)
    interval = 1.0 / rate
    base_seq = idx * _FLOW_STRIDE
    sent = 0
    local = 0
    start = time.monotonic()
    deadline = start + duration
    next_send = start
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if now < next_send:
            time.sleep(min(next_send - now, 0.002))
            continue
        wire = base_seq + local
        msg = _SEQ.pack(wire) + pad
        with lock:
            inflight[wire] = time.monotonic()
        try:
            sock.sendto(msg, (host, port))
            sent += 1
        except OSError:
            with lock:
                inflight.pop(wire, None)
        local += 1
        next_send += interval

    time.sleep(drain)        # let late echoes arrive
    stop.set()
    rx.join(timeout=1.0)
    try:
        sock.close()
    except OSError:
        pass
    out[idx] = {'sent': sent, 'received': len(rtts), 'rtts': rtts}


def soak(host, port, flows, rate, duration, payload_size=64, drain=1.0):
    out = [None] * flows
    threads = [
        threading.Thread(
            target=_run_flow,
            args=(host, port, rate, duration, payload_size, drain, out, i),
        )
        for i in range(flows)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results = [r for r in out if r is not None]   # a dead flow thread leaves None
    sent = sum(r['sent'] for r in results)
    received = sum(r['received'] for r in results)
    all_rtts = sorted(rtt for r in results for rtt in r['rtts'])
    loss = sent - received
    return {
        'target': '%s:%d' % (host, port),
        'flows': flows,
        'flows_completed': len(results),
        'rate_pps_per_flow': rate,
        'duration_s': duration,
        'payload_bytes': payload_size,
        'sent': sent,
        'received': received,
        'loss': loss,
        'loss_pct': round(100.0 * loss / sent, 4) if sent else None,
        'p50_ms': round(_percentile(all_rtts, 50) * 1000, 4) if all_rtts else None,
        'p99_ms': round(_percentile(all_rtts, 99) * 1000, 4) if all_rtts else None,
        'max_ms': round(all_rtts[-1] * 1000, 4) if all_rtts else None,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True, help='HOST:PORT of the UDP echo')
    p.add_argument('--flows', type=int, default=10, help='concurrent flows')
    p.add_argument('--rate', type=int, default=100,
                   help='datagrams/s per flow')
    p.add_argument('--duration', type=float, default=30.0, help='seconds')
    p.add_argument('--payload', type=int, default=64, help='datagram bytes')
    p.add_argument('--drain', type=float, default=1.0,
                   help='seconds to wait for late echoes after sending stops')
    args = p.parse_args(argv)

    host, _, port = args.target.rpartition(':')
    if not host or not port:
        p.error('--target must be HOST:PORT')
    if args.rate <= 0 or args.flows <= 0 or args.duration <= 0:
        p.error('--rate, --flows, and --duration must be positive')
    metrics = soak(
        host, int(port), args.flows, args.rate, args.duration,
        payload_size=args.payload, drain=args.drain,
    )
    json.dump(metrics, sys.stdout, indent=2)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
