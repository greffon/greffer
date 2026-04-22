"""Monitor worker — poll docker status for each greffon instance, report changes.

Ports ``apps/utils/greffon/monitoring.py:monitor_status`` to asyncio.

**Intentional deviation from legacy:** the legacy sync version places its
``try/except`` *outside* the while loop, so the first per-tick exception
kills monitoring permanently (monitoring silently goes dead, no manager
updates arrive ever again). The async version catches per-tick and
continues — a deliberate bug fix. See hld-workers.md § Risks.
"""
from __future__ import annotations

import asyncio
import logging
import os

import anyio
from fastapi import FastAPI

from app.settings import Settings

logger = logging.getLogger("greffer")


async def monitor_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    prev_status: dict[str, str] = {}
    try:
        while True:
            logger.info("monitoring begin")
            try:
                await anyio.to_thread.run_sync(
                    _one_monitor_tick, settings, prev_status
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Deviation from legacy: one bad tick does not kill the
                # worker. Next iteration tries again after the same delay.
                logger.exception("monitor tick failed; continuing")
            await asyncio.sleep(settings.monitor_interval)
    except asyncio.CancelledError:
        logger.info("monitor cancelled")
        raise


def _one_monitor_tick(settings: Settings, prev_status: dict[str, str]) -> None:
    """Run one monitoring pass. Mutates ``prev_status`` in place."""
    # Imported lazily so unit tests can mock before the docker SDK
    # initializes its from_env() client at import.
    from apps.utils.docker import compose
    from apps.utils.greffon import base_server

    greffon_dir = str(settings.greffon_path)
    for greffon_id in os.listdir(greffon_dir):
        status = compose.get_status(greffon_id)["status"]
        if prev_status.get(greffon_id) != status:
            base_server.change_status(greffon_id, status)
        prev_status[greffon_id] = status
