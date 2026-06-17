"""Greffer self-update v2: the detached, socket-only updater engine.

This package is the verify-then-pull + per-container recreate logic that the
signed ``greffon/greffer-updater`` image runs when the manager triggers a remote
update. Per ``docs/features/greffer-self-update/hld-v2-per-container-recreate.md``
it talks ONLY to the host docker socket (no compose file): it discovers the
running greffon stack by compose service label, verify-then-pulls every
``greffon/*`` image (resolve ``:latest`` to its digest, cosign-verify it
repo-bound, pull by digest, then point local ``:latest`` at it), recreates each
container in order carrying its config (the section 8 fidelity rule), health-gates
``/readyz``, and rolls back on failure.

Every verification fails closed: the whole stack is verified BEFORE any tag moves
or any container is recreated, so a failure leaves the node on its current,
already-trusted image. (The ``:latest`` model deliberately drops the
``min_supported`` floor + signed-manifest + ratchet of the digest-pin design,
HLD section 7; cosign signature verification is kept via verify-then-pull.)

Modules: ``recreate`` (Docker-API primitives + fidelity recreate), ``provenance``
(digest resolve + cosign verify + version label), ``gate`` (socket ``/readyz``
gate), ``engine`` (orchestration).
"""
