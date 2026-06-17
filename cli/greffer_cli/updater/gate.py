"""Socket-only `/readyz` health gate for the v2 ``:latest`` updater (HLD §9).

Mirrors ``greffer_cli.update.health_gate`` but reaches the recreated greffer
over the docker socket (``docker exec <name>``, via ``recreate``) instead of
``docker compose exec``, since this path has no compose file. The decision logic
is identical and reuses the pure pieces from ``update`` (``parse_readyz``, the
``GATE_*`` outcomes, the tolerable-degraded set). Containers are addressed by
NAME, which is stable across recreate (the recreate gives the container a new
id but the same name).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from .. import update
from . import recreate

logger = logging.getLogger("greffer-updater")


def health_gate(
    greffer_name: str, *, greffer_id: str | None, applied_image_id: str | None,
    service_names: list[str], timeout: float, poll_interval: float = 2.0,
    check_version: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Poll `/readyz` until the recreated stack is healthy or a failure is
    decided. ``GATE_READY`` requires all of: `/readyz` ``ready`` with a matching
    ``id``, the running greffer image == ``applied_image_id`` (the verified new
    digest actually applied), and every stack container running.
    ``degraded: registration_pending`` is awaited; a wrong id, ``fatal``, any
    other ``degraded`` reason, a crash-loop, or the timeout fail.

    ``check_version=False`` (rollback) skips the applied-image check: rollback
    restores the prior image, so any ready + matching id + all-running passes.
    """
    deadline = now() + timeout
    base_restarts = recreate.restart_count(greffer_name)
    while now() < deadline:
        r = update.parse_readyz(recreate.exec_readyz(greffer_name))
        if r.ok:
            if greffer_id and r.id and r.id != greffer_id:
                return update.GATE_WRONG_ID
            if r.status == "fatal":
                return update.GATE_FATAL
            if r.status == "degraded" and any(
                reason not in update.TOLERABLE_DEGRADED_REASONS for reason in r.reasons
            ):
                return update.GATE_DEGRADED_OTHER
            if r.status == "ready":
                if check_version:
                    running = recreate.container_image_id_by_name(greffer_name)
                    if not (running and applied_image_id and running == applied_image_id):
                        return update.GATE_NOT_APPLIED
                if all(recreate.container_running(n) for n in service_names):
                    return update.GATE_READY
                # greffer ready but a sibling (nginx/sidecar) is not up yet;
                # keep polling, the timeout is the backstop.
        # Climbing restart count past the post-recreate baseline = crash loop.
        if recreate.restart_count(greffer_name) - base_restarts > 1:
            return update.GATE_CRASH_LOOP
        sleep(poll_interval)
    return update.GATE_TIMEOUT
