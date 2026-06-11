"""Heartbeat worker — push greffer liveness to the manager every interval.

Greffer-observability epic, Feature #1. Each beat POSTs the full instance-status
map plus version/uptime/disk/seq to ``/api/greffer/<id>/heartbeat/``, so a dead
or unreachable greffer is distinguishable from a healthy idle one. The beat is
sent unconditionally: if status collection fails it goes out ``degraded`` with an
empty map, so the manager sees "alive but degraded" rather than "gone". A 403
asks the register supervisor to re-run registration (the manager rejected our
token). See docs/features/greffer-observability/hld-heartbeat.md.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone

import anyio
import requests
from fastapi import FastAPI

from app.settings import Settings
from app.workers.status_collect import collect_status_map

logger = logging.getLogger("greffer")

_HTTP_TIMEOUT_SECONDS = 10.0


async def heartbeat_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    seq = 0
    try:
        while True:
            seq += 1
            try:
                status_code = await anyio.to_thread.run_sync(
                    _one_heartbeat, app, seq, abandon_on_cancel=True
                )
                if status_code == 403:
                    logger.warning(
                        "heartbeat rejected (403); requesting re-register"
                    )
                    app.state.reregister_requested.set()
            except asyncio.CancelledError:
                raise
            except (requests.ConnectionError, requests.Timeout):
                logger.info(
                    "manager unreachable for heartbeat; retrying next interval"
                )
            except Exception:
                logger.exception("heartbeat failed; continuing")
            await asyncio.sleep(settings.heartbeat_interval)
    except asyncio.CancelledError:
        logger.info("heartbeat cancelled")
        raise


def _collect_or_reuse(
    app: FastAPI, settings: Settings
) -> tuple[dict[str, str], bool, list[str]]:
    """Return ``(status_map, degraded, reasons)``. Reuse the monitor's recent
    sweep if fresh; else collect. On collection failure, return degraded with an
    empty map (the manager must not treat an empty degraded map as "everything
    missing")."""
    # Reuse the monitor's sweep if it is recent. The monitor refreshes every
    # monitor_interval, so judge freshness against monitor_interval + the
    # heartbeat period: with equal 5s intervals a window of just
    # heartbeat_interval would frequently miss the last tick and re-sweep.
    cached = getattr(app.state, "status_map", None)
    window = settings.monitor_interval + settings.heartbeat_interval
    if cached and (time.monotonic() - cached["at"]) < window:
        return cached["map"], False, []
    try:
        return collect_status_map(settings), False, []
    except Exception:
        logger.exception("heartbeat status collection failed; sending degraded")
        return {}, True, ["docker_unreachable"]


def _disk_free_bytes(settings: Settings) -> int | None:
    try:
        return shutil.disk_usage(str(settings.greffon_path)).free
    except OSError:
        return None


def _one_heartbeat(app: FastAPI, seq: int) -> int:
    """Build and POST one heartbeat. Returns the HTTP status code; network
    errors propagate to the worker loop."""
    settings: Settings = app.state.settings
    status_map, degraded, reasons = _collect_or_reuse(app, settings)
    payload = {
        "boot_id": app.state.boot_id,
        "seq": seq,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "interval": settings.heartbeat_interval,
        "version": settings.greffer_version,
        "uptime_s": int(time.monotonic() - app.state.started_at),
        "degraded": degraded,
        "reasons": reasons,
        "disk_free_bytes": _disk_free_bytes(settings),
        "instances": status_map,
    }
    res = requests.post(
        f"{settings.greffon_base_server}/api/greffer/{settings.greffer_id}/heartbeat/",
        json=payload,
        headers={"X-Greffer-Token": app.state.greffer_token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    return res.status_code
