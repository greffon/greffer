#!/usr/bin/env python3
"""Apply the tunnel-mode UDP acceptance bar to a relay run vs a baseline run.

Reads two JSON metrics objects produced by ``udp_soak.py``: the run through the
rathole relay and the direct (proxy-mode) baseline. The relay's added latency is
``relay.p99_ms - baseline.p99_ms``. Exits 0 if the relay is within the bar, 1
otherwise, so it drops straight into a CI / load-test step.

The default thresholds are STARTING POINTS, not gospel: per the design note the
bar is meant to be set from the first real run on a Linux greffer. They encode
"a few percent loss and a modest p99 add over the proxy baseline is acceptable
for the tolerant-UDP apps tunnel mode targets" (WireGuard etc. stay proxy-mode).
"""

import argparse
import json
import sys


def evaluate(relay, baseline, max_loss_pct, max_p99_added_ms,
             max_baseline_loss_pct):
    loss = relay.get('loss_pct')
    base_loss = baseline.get('loss_pct')
    added = None
    if relay.get('p99_ms') is not None and baseline.get('p99_ms') is not None:
        added = round(relay['p99_ms'] - baseline['p99_ms'], 4)

    # A lossy baseline means the echo / host (not the relay) is the bottleneck,
    # so the relay numbers are not a valid measurement: judging the relay from
    # this run would be measuring the test rig, not rathole. Lower the rate.
    invalid = base_loss is None or base_loss > max_baseline_loss_pct
    invalid_reason = None
    if invalid:
        invalid_reason = (
            'baseline loss_pct=%s exceeds max %s: the test setup (echo/host) is '
            'saturated, so the relay cannot be judged. Lower RATE/FLOWS and '
            're-run.' % (base_loss, max_baseline_loss_pct))

    failures = []
    if loss is None or loss > max_loss_pct:
        failures.append('relay loss_pct=%s exceeds max %s' % (loss, max_loss_pct))
    if added is None or added > max_p99_added_ms:
        failures.append(
            'relay p99_added_ms=%s exceeds max %s' % (added, max_p99_added_ms))

    return {
        'valid': not invalid,
        'invalid_reason': invalid_reason,
        'baseline_loss_pct': base_loss,
        'relay_loss_pct': loss,
        'relay_p99_ms': relay.get('p99_ms'),
        'baseline_p99_ms': baseline.get('p99_ms'),
        'p99_added_ms': added,
        'bar': {
            'max_loss_pct': max_loss_pct,
            'max_p99_added_ms': max_p99_added_ms,
            'max_baseline_loss_pct': max_baseline_loss_pct,
        },
        # Pass requires a VALID run (baseline healthy) AND the relay within bar.
        'pass': (not invalid) and (not failures),
        'failures': failures,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--relay', required=True, help='relay metrics JSON file')
    p.add_argument('--baseline', required=True,
                   help='direct/proxy-mode baseline metrics JSON file')
    p.add_argument('--max-loss-pct', type=float, default=2.0)
    p.add_argument('--max-p99-added-ms', type=float, default=50.0)
    p.add_argument('--max-baseline-loss-pct', type=float, default=1.0,
                   help='above this the baseline is saturated and the run is '
                        'treated as an invalid measurement, not a relay failure')
    args = p.parse_args(argv)

    with open(args.relay) as fh:
        relay = json.load(fh)
    with open(args.baseline) as fh:
        baseline = json.load(fh)

    result = evaluate(
        relay, baseline, args.max_loss_pct, args.max_p99_added_ms,
        args.max_baseline_loss_pct)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write('\n')
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
