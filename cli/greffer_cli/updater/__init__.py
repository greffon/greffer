"""Greffer self-update v2: the detached updater engine.

This package is the verification + recreate logic that the signed
``greffon/greffer-updater`` image runs when the manager triggers a remote
update. It enforces the trust model in
``docs/features/greffer-self-update/v2-image-provenance-and-trust-model.md``
(cosign verify + repo-binding + digest pin + the anti-replay ``min_supported``
floor) and then reuses the v1 ``greffer_cli`` compose engine to recreate the
node, health-gate on ``/readyz``, and roll back on failure.

Every check fails closed: any verification or floor failure aborts BEFORE the
node is recreated, leaving it on its current, already-trusted image.
"""
