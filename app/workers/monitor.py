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
from datetime import datetime, timezone

import anyio
import requests
from fastapi import FastAPI

from app.diagnostics import diag
from app.settings import Settings
from app.workers.status_collect import collect_status_map

logger = logging.getLogger("greffer")

_HTTP_TIMEOUT_SECONDS = 10.0


async def monitor_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    prev_status: dict[str, str] = {}
    try:
        while True:
            logger.info("monitoring begin")
            try:
                # Re-read the token each tick (not snapshotted at startup) so a
                # re-register rotation (reregister_worker mutates
                # app.state.greffer_token) is honored on the legacy callback,
                # matching the heartbeat worker. Otherwise post-rotation
                # callbacks would 403 once GREFFER_CALLBACK_ENFORCE_TOKEN is on.
                token: str = app.state.greffer_token
                # Stamp captured_at BEFORE the tick, not after. _one_monitor_tick
                # both collects docker state and runs per-instance callbacks; a
                # start committing during that window would otherwise be older
                # than an after-the-fact captured_at, letting a reused (stale)
                # map's timestamp postdate the start and defeat the manager's
                # STARTING grace. Stamping before the sweep keeps captured_at <=
                # the actual collection time, so the grace stays fail-safe.
                captured_at = datetime.now(timezone.utc).isoformat()
                # abandon_on_cancel=True — lifespan shutdown returns
                # immediately even if a tick is mid-docker-API or mid-HTTP
                # call. Inner HTTP call carries timeout=10 so the thread
                # also can't hang forever.
                _tick_start = time.monotonic()
                status_map = await anyio.to_thread.run_sync(
                    _one_monitor_tick,
                    settings,
                    prev_status,
                    token,
                    abandon_on_cancel=True,
                )
                # Diagnostic counter (Feature #4 fast-follow): tick duration +
                # instance count, at DEBUG so the 5s cadence doesn't spam INFO.
                diag("monitor_tick", level=logging.DEBUG, outcome="ok",
                     duration_ms=round((time.monotonic() - _tick_start) * 1000),
                     instances=len(status_map))
                # Publish the sweep for the heartbeat worker to reuse, so the
                # two timers don't each hit docker (greffer-observability epic).
                # ``at`` is monotonic (freshness); ``captured_at`` is the wall
                # clock the heartbeat sends so a reused map isn't claimed as
                # freshly captured (keeps the manager's STARTING-grace honest).
                app.state.status_map = {
                    "map": status_map,
                    "at": time.monotonic(),
                    "captured_at": captured_at,
                }
            except asyncio.CancelledError:
                raise
            except Exception:
                # Deviation from legacy: one bad tick does not kill the worker.
                # Reaching here means status COLLECTION failed (a docker outage;
                # transition-callback failures are caught per-instance and do
                # not abort the tick). Invalidate the published sweep so the
                # heartbeat collects fresh and surfaces a degraded beat promptly
                # rather than reusing the last healthy map for the window.
                app.state.status_map = None
                diag("monitor_tick", level=logging.ERROR, outcome="error")
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
            try:
                code = _report_status_change(settings, greffon_id, status, token)
            except (requests.ConnectionError, requests.Timeout):
                # The manager is briefly unreachable for the callback. Don't
                # abort the tick (which would discard a good docker sweep and
                # leave prev_status half-updated) and don't advance prev_status
                # for this instance, so the transition is retried next tick.
                diag("status_callback", op="status", outcome="unreachable",
                     greffon_id=greffon_id)
                continue
            if code >= 400:
                # The manager REJECTED the callback (e.g. 403 from a stale/rotated
                # token once GREFFER_CALLBACK_ENFORCE_TOKEN is on). requests.post
                # does NOT raise on 4xx/5xx, so without this the transition would
                # be treated as delivered and lost. Leave prev_status unchanged so
                # it retries next tick, after a heartbeat-driven re-register
                # restages the token.
                diag("status_callback", level=logging.WARNING, op="status",
                     outcome="rejected", status_code=code, greffon_id=greffon_id)
                continue
        prev_status[greffon_id] = status
    return status_map


def _report_status_change(
    settings: Settings, greffon_id: str, status: str, token: str
) -> int:
    """POST a status change to the manager; return the HTTP status code.

    Inlined from the now-deleted ``apps/utils/greffon/base_server.py``'s
    ``change_status``. Sends ``X-Greffer-Token`` so the manager can enforce auth
    on this callback once GREFFER_CALLBACK_ENFORCE_TOKEN flips on (old managers
    ignore the header). The caller inspects the code so a rejected (non-2xx)
    callback isn't treated as a delivered transition.
    """
    res = requests.post(
        f"{settings.greffon_base_server}/api/greffer/instances/{greffon_id}/",
        json={"status": status},
        headers={"X-Greffer-Token": token},
        verify=settings.greffer_ssl_verify,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    return res.status_code
