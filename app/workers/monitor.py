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
import time

import anyio
import requests
from fastapi import FastAPI

from app.settings import Settings
from app.workers.status_collect import collect_status_map

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
                status_map = await anyio.to_thread.run_sync(
                    _one_monitor_tick,
                    settings,
                    prev_status,
                    token,
                    abandon_on_cancel=True,
                )
                # Publish the sweep for the heartbeat worker to reuse, so the
                # two timers don't each hit docker (greffer-observability epic).
                app.state.status_map = {
                    "map": status_map, "at": time.monotonic()}
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
    settings: Settings, prev_status: dict[str, str], token: str
) -> dict[str, str]:
    """Run one monitoring pass. Mutates ``prev_status`` in place and returns the
    full collected status map (reused by the heartbeat). Dotfile skipping lives
    in ``collect_status_map``."""
    status_map = collect_status_map(settings)
    for greffon_id, status in status_map.items():
        if prev_status.get(greffon_id) != status:
            _report_status_change(settings, greffon_id, status, token)
        prev_status[greffon_id] = status
    return status_map


def _report_status_change(
    settings: Settings, greffon_id: str, status: str, token: str
) -> None:
    """POST a status change to the manager.

    Inlined from the now-deleted ``apps/utils/greffon/base_server.py``'s
    ``change_status``. Sends ``X-Greffer-Token`` so the manager can enforce auth
    on this callback once GREFFER_CALLBACK_ENFORCE_TOKEN flips on (old managers
    ignore the header).
    """
    requests.post(
        f"{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/",
        json={"status": status},
        headers={"X-Greffer-Token": token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
