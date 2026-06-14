"""Self-heal watchdog (greffer-observability epic, Feature #3).

Plain ``docker compose`` (non-Swarm) does NOT restart a container on an
unhealthy healthcheck; ``restart: unless-stopped`` fires only on process exit.
So the greffer watches its OWN readiness in-process and, when a FATAL condition
(see ``app/readiness.py``) is sustained past the grace window, exits the uvicorn
process so the restart policy recovers it.

Safety (the watchdog is on by default, so the safety lives in the logic, not a
flag): only FATAL conditions ever trigger an exit; degraded states (e.g.
registration pending acceptance) never do, so a greffer awaiting acceptance is
never restart-looped. The grace window rides out transient docker blips, and a
condition that clears before grace expires resets the timer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import anyio
from fastapi import FastAPI

from app.readiness import evaluate_readiness
from app.settings import Settings

logger = logging.getLogger("greffer")


def _terminate() -> None:
    """Ask uvicorn to shut down gracefully so the container's restart policy
    brings up a fresh process. SIGTERM (not ``os._exit``) so lifespan teardown
    and worker cancellation run rather than leaving in-flight compose ops or
    file writes half-done. Isolated in a function so tests can patch it."""
    logger.critical("watchdog: sending SIGTERM to self (pid=%s) for restart",
                    os.getpid())
    os.kill(os.getpid(), signal.SIGTERM)


async def watchdog_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    fatal_since: float | None = None
    try:
        while True:
            await asyncio.sleep(settings.greffer_watchdog_interval)
            # Offload the docker-pinging evaluation so a hung daemon cannot
            # block the event loop the other workers share.
            readiness = await anyio.to_thread.run_sync(evaluate_readiness, app)
            if not readiness.fatal:
                if fatal_since is not None:
                    logger.info(
                        "watchdog: fatal condition cleared before grace "
                        "expired; not restarting")
                fatal_since = None
                continue
            now = time.monotonic()
            if fatal_since is None:
                # First fatal observation: start the grace clock, do not act
                # yet (rides out a transient blip).
                fatal_since = now
                logger.error(
                    "watchdog: FATAL readiness %s; will restart if sustained "
                    "%ss", readiness.reasons, settings.greffer_watchdog_grace)
            elif now - fatal_since >= settings.greffer_watchdog_grace:
                logger.critical(
                    "watchdog: FATAL readiness sustained >= %ss (%s); exiting "
                    "uvicorn for restart", settings.greffer_watchdog_grace,
                    readiness.reasons)
                _terminate()
                return
    except asyncio.CancelledError:
        logger.info("watchdog cancelled")
        raise
