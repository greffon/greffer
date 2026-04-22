from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.workers import start_workers, stop_workers

logger = logging.getLogger("greffer")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — start the three background workers if enabled.

    ``greffer_workers_enabled`` defaults to False so unit tests don't
    accidentally spawn real workers. Production sets
    ``GREFFER_WORKERS_ENABLED=true`` in docker-compose.yml.
    """
    if not app.state.settings.greffer_workers_enabled:
        logger.info("workers disabled (GREFFER_WORKERS_ENABLED unset)")
        yield
        return
    tasks = start_workers(app)
    logger.info("started %d background workers", len(tasks))
    try:
        yield
    finally:
        await stop_workers(tasks)
