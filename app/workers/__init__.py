"""Background workers — greffer lifecycle tasks running under FastAPI lifespan.

Lifecycle workers:

* ``register_worker`` — one-shot; POSTs registration + polls for cert.
* ``monitor_worker`` — forever loop; reports greffon instance status changes.
* ``crl_sync_worker`` — forever loop; fetches updated CRL periodically.
* ``heartbeat_worker`` — forever loop; pushes greffer liveness to the manager
  (greffer-observability epic).
* ``reregister_worker`` — supervisor; re-runs registration when the heartbeat
  hits a 403 (the manager rejected our token).

Startup is gated by ``Settings.greffer_workers_enabled`` (env var
``GREFFER_WORKERS_ENABLED``, default False) so unit tests don't
accidentally spawn real workers. Production sets it to ``true`` in
``docker-compose.yml``.

Single-worker uvicorn only. Multi-worker would spawn N copies of each poller
per container, each with a distinct token, fighting over manager state.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from app.workers.crl import crl_sync_worker
from app.workers.heartbeat import heartbeat_worker
from app.workers.monitor import monitor_worker
from app.workers.register import register_worker, reregister_worker

logger = logging.getLogger("greffer")


def start_workers(app: FastAPI) -> list[asyncio.Task]:
    return [
        asyncio.create_task(register_worker(app), name="greffer-register"),
        asyncio.create_task(monitor_worker(app), name="greffer-monitor"),
        asyncio.create_task(crl_sync_worker(app), name="greffer-crl-sync"),
        asyncio.create_task(heartbeat_worker(app), name="greffer-heartbeat"),
        asyncio.create_task(
            reregister_worker(app), name="greffer-reregister"),
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
