"""Background workers — greffer lifecycle tasks running under FastAPI lifespan.

Three workers port the Django daemon threads at ``apps/controller/views.py:14-22``:

* ``register_worker`` — one-shot; POSTs registration + polls for cert.
* ``monitor_worker`` — forever loop; reports greffon instance status changes.
* ``crl_sync_worker`` — forever loop; fetches updated CRL periodically.

Startup is gated by ``Settings.workers_enabled`` (default False) so that during
the parallel-tree phase the Django runtime keeps owning the real register +
status callbacks; flipping the flag is feature #4's atomic cutover act.

Single-worker uvicorn only. Multi-worker would spawn N × 3 pollers per
container, each with a distinct token, fighting over manager state.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from app.workers.crl import crl_sync_worker
from app.workers.monitor import monitor_worker
from app.workers.register import register_worker

logger = logging.getLogger("greffer")


def start_workers(app: FastAPI) -> list[asyncio.Task]:
    return [
        asyncio.create_task(register_worker(app), name="greffer-register"),
        asyncio.create_task(monitor_worker(app), name="greffer-monitor"),
        asyncio.create_task(crl_sync_worker(app), name="greffer-crl-sync"),
    ]


async def stop_workers(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for t, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            logger.warning("worker %s ended with %r", t.get_name(), result)
