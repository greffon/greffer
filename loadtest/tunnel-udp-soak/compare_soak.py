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


def evaluate(relay, baseline, max_loss_pct, max_p99_added_ms):
    loss = relay.get('loss_pct')
    added = None
    if relay.get('p99_ms') is not None and baseline.get('p99_ms') is not None:
        added = round(relay['p99_ms'] - baseline['p99_ms'], 4)

    failures = []
    if loss is None or loss > max_loss_pct:
        failures.append('relay loss_pct=%s exceeds max %s' % (loss, max_loss_pct))
    if added is None or added > max_p99_added_ms:
        failures.append(
            'relay p99_added_ms=%s exceeds max %s' % (added, max_p99_added_ms))

    return {
        'relay_loss_pct': loss,
        'relay_p99_ms': relay.get('p99_ms'),
        'baseline_p99_ms': baseline.get('p99_ms'),
        'p99_added_ms': added,
        'bar': {
            'max_loss_pct': max_loss_pct,
            'max_p99_added_ms': max_p99_added_ms,
        },
        'pass': not failures,
        'failures': failures,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--relay', required=True, help='relay metrics JSON file')
    p.add_argument('--baseline', required=True,
                   help='direct/proxy-mode baseline metrics JSON file')
    p.add_argument('--max-loss-pct', type=float, default=2.0)
    p.add_argument('--max-p99-added-ms', type=float, default=50.0)
    args = p.parse_args(argv)

    with open(args.relay) as fh:
        relay = json.load(fh)
    with open(args.baseline) as fh:
        baseline = json.load(fh)

    result = evaluate(
        relay, baseline, args.max_loss_pct, args.max_p99_added_ms)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write('\n')
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
