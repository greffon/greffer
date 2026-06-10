# Tunnel-mode UDP soak / load test

The acceptance-bar test for relaying **UDP** through the rathole tunnel
(l4-network-exposure, UDP increment). It answers: is the rathole UDP relay's
added latency and datagram loss acceptable for the tolerant-UDP services tunnel
mode targets? Feasibility is already cleared (rathole#75, the historical UDP
flapping bug, was fixed in v0.3.3 and is in the pinned v0.5.0). This is the
**bar-setting** run, not a go/no-go.

It is the "rathole UDP load test (greffer step)" from
`docs/features/l4-network-exposure/followup-tunnel-udp-ungate.md`. Its result
gates flipping `L4_TUNNEL_UDP_ENABLED` in the manager.

## What it does

```
        host driver (udp_soak.py)
          |                 |
   :7777 (direct)      :10000 (relay)
          |                 |
        [echo] <---- [rathole-client] <== tunnel ==> [rathole-server]
```

Two runs of the same N concurrent UDP flows against the same echo:

- **baseline**: straight to the echo's published UDP port (the proxy-mode path,
  no relay).
- **relay**: through `rathole-server`'s UDP data port, over the tunnel, to the
  same echo.

`compare_soak.py` reports the relay's loss and its **p99 added latency** over the
baseline, and exits non-zero if either exceeds the bar.

## Run it (Linux Docker Engine)

```sh
./run-soak.sh
# tune: FLOWS, RATE (per-flow pps), DURATION, MAX_LOSS_PCT, MAX_P99_ADDED_MS
FLOWS=50 RATE=500 DURATION=300 ./run-soak.sh
```

Run on a **Linux Docker Engine** host. Docker Desktop masks loopback-vs-relay
behaviour (Gap 0), so its numbers are not representative. A CI runner is fine for
a smoke check, but a real latency/loss bar should come from a quiet host (shared
CI runners add their own jitter).

The pieces also run standalone:

```sh
python3 udp_soak.py --target HOST:PORT --flows 20 --rate 200 --duration 60
python3 compare_soak.py --relay relay.json --baseline baseline.json
```

## The bar

Defaults (`MAX_LOSS_PCT=2.0`, `MAX_P99_ADDED_MS=50`) are **starting points**. Per
the design note, set the real bar from the first run: pick thresholds that a
healthy relay clears with margin, so the test catches regressions, not noise.
WireGuard and other latency-sensitive UDP stay on proxy mode regardless, so the
bar is sized for tolerant UDP (game servers, telemetry, etc.).

`compare_soak.py` also guards against a **saturated test rig**: if the baseline
(direct) run itself loses more than `MAX_BASELINE_LOSS_PCT` (default 1%), the
echo or host is the bottleneck, the relay numbers are not a valid measurement,
and the run is reported `valid: false` (lower `RATE`/`FLOWS` and re-run). This
stops a slow rig from being misread as a relay failure.

If the relay cannot clear a sane bar, tunnel mode ships TCP-only and UDP stays
gated, which is the design's documented fallback.

## Validation status

- `udp_soak.py` and `compare_soak.py` are stdlib-only and **validated locally**
  (the driver against `udp_echo.py`: correct sent/received/loss and p50/p99; the
  comparator's pass/fail + exit codes).
- The **rig** (`docker-compose.soak.yml` + the rathole `*.toml`) needs a Linux
  Docker host with the `greffon/rathole-server` image to run end to end; treat
  the first run as bring-up (the rathole client/server wiring is the part to
  confirm live, as with the #145 relay e2e).

## Files

| File | Role |
|------|------|
| `udp_soak.py` | load + measurement driver (N flows, RTT, loss, p50/p99) |
| `compare_soak.py` | applies the bar to a relay run vs a baseline run |
| `udp_echo.py` | stdlib UDP echo (rig echo service + local driver validation) |
| `docker-compose.soak.yml` | the rig: echo + rathole-server + rathole-client |
| `server.toml` / `client.toml` | rathole UDP service config |
| `run-soak.sh` | orchestrates baseline + relay + bar; exit code is the result |
