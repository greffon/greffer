from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.workers import start_workers, stop_workers
from apps.utils.docker.observe import shutdown_stats_pool

logger = logging.getLogger("greffer")


# ``_publish_token_to_sidecar`` was deleted in the v3 cleanup PR. The
# sidecar no longer makes outbound calls to the manager (push model
# replaced the polling sidecar — see greffer#28's
# ``POST /api/controller/tunnel-config/`` endpoint that manager pushes
# to). Without an agent.py, there's no consumer for the
# /run/tunnel-secrets/greffer-token file; the whole token-handoff
# machinery is gone. The mTLS-migration follow-up tracked in the v2
# epic is obsolete in v3 because there is no sidecar→manager leg.


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — start the three background workers if enabled.

    ``greffer_workers_enabled`` defaults to False so unit tests don't
    accidentally spawn real workers. Production sets
    ``GREFFER_WORKERS_ENABLED=true`` in docker-compose.yml.
    """
    # Tear the shared stats pool down on the way out regardless of which path
    # we take below, so a daemon-hung ``container.stats()`` worker never stalls
    # process exit / a watchdog restart (see shutdown_stats_pool).
    try:
        if not app.state.settings.greffer_workers_enabled:
            logger.info("workers disabled (GREFFER_WORKERS_ENABLED unset)")
            yield
            return
        tasks = start_workers(app)
        # Expose the task handles by name so /readyz and the watchdog can tell
        # a live long-lived worker from a crashed one (Feature #3). Set
        # synchronously before the first ``yield``, so it is populated before
        # any task runs.
        app.state.worker_tasks = {t.get_name(): t for t in tasks}
        logger.info("started %d background workers", len(tasks))
        try:
            yield
        finally:
            await stop_workers(tasks)
    finally:
        shutdown_stats_pool()
