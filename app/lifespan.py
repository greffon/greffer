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

    ``workers_enabled`` defaults to False. Feature #4's cutover PR flips
    it to True at the moment Django is removed, so the FastAPI workers
    never run alongside Django's daemon threads in the same container.
    """
    if not app.state.settings.workers_enabled:
        logger.info("workers disabled (GREFFER_WORKERS_ENABLED unset)")
        yield
        return
    tasks = start_workers(app)
    logger.info("started %d background workers", len(tasks))
    try:
        yield
    finally:
        await stop_workers(tasks)
