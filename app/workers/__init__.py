"""Background workers — greffer lifecycle tasks running under FastAPI lifespan.

Two workers port the Django daemon threads at ``apps/controller/views.py:14-22``:

* ``register_worker`` — one-shot; POSTs registration + polls for cert.
* ``monitor_worker`` — forever loop; reports greffon instance status changes.

(A third legacy worker, ``crl_sync_worker``, copied the manager's CRL
into the nginx container. Removed: no nginx config ever loaded the file
(``ssl_crl`` requires client-cert verification, which this nginx does
not do), so the sync had no effect. Revocation enforcement is deferred
to the platform's planned step-ca migration.)

Startup is gated by ``Settings.greffer_workers_enabled`` (env var
``GREFFER_WORKERS_ENABLED``, default False) so unit tests don't
accidentally spawn real workers. Production sets it to ``true`` in
``docker-compose.yml``.

Single-worker uvicorn only. Multi-worker would spawn N × 2 pollers per
container, each with a distinct token, fighting over manager state.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from app.workers.monitor import monitor_worker
from app.workers.register import register_worker

logger = logging.getLogger("greffer")


def start_workers(app: FastAPI) -> list[asyncio.Task]:
    return [
        asyncio.create_task(register_worker(app), name="greffer-register"),
        asyncio.create_task(monitor_worker(app), name="greffer-monitor"),
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
