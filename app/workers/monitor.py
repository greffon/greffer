"""Monitor worker — poll docker status for each greffon instance, report changes.

Ports ``apps/utils/greffon/monitoring.py:monitor_status`` (since deleted in
the cutover) to asyncio.

**Intentional deviation from legacy:** the legacy sync version placed its
``try/except`` *outside* the while loop, so the first per-tick exception
killed monitoring permanently (no further manager callbacks until
restart). This async version catches per-tick and continues — a
deliberate bug fix. See hld-workers.md § Risks and hld-cutover.md.
"""
from __future__ import annotations

import asyncio
import logging
import os

import anyio
import requests
from fastapi import FastAPI

from app.settings import Settings

logger = logging.getLogger("greffer")

_HTTP_TIMEOUT_SECONDS = 10.0


async def monitor_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    token: str = app.state.greffer_token
    prev_status: dict[str, str] = {}
    try:
        while True:
            logger.info("monitoring begin")
            try:
                # abandon_on_cancel=True — lifespan shutdown returns
                # immediately even if a tick is mid-docker-API or mid-HTTP
                # call. Inner HTTP call carries timeout=10 so the thread
                # also can't hang forever.
                await anyio.to_thread.run_sync(
                    _one_monitor_tick,
                    settings,
                    token,
                    prev_status,
                    abandon_on_cancel=True,
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


def _one_monitor_tick(
    settings: Settings, token: str, prev_status: dict[str, str]
) -> None:
    """Run one monitoring pass. Mutates ``prev_status`` in place."""
    # Imported lazily so unit tests can mock before the docker SDK
    # initializes its from_env() client at import.
    from apps.utils.docker import compose

    greffon_dir = str(settings.greffon_path)
    for greffon_id in os.listdir(greffon_dir):
        status = compose.get_status(greffon_id)["status"]
        if prev_status.get(greffon_id) != status:
            _report_status_change(settings, token, greffon_id, status)
        prev_status[greffon_id] = status


def _report_status_change(
    settings: Settings, token: str, greffon_id: str, status: str
) -> None:
    """POST a status change to the manager. Carries ``X-GREFFON-TOKEN``
    because the manager's greffon_status_changed endpoint authenticates
    the greffer by that header and cross-checks ownership of the
    instance id against the authenticated Greffer.
    """
    requests.post(
        f"{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/",
        json={"status": status},
        headers={"X-GREFFON-TOKEN": token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
