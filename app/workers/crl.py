"""CRL sync worker — periodically fetch the CRL from the manager.

Ports ``apps/utils/greffon/base_server.py:sync_crl()``. Legacy order is
**sleep-then-fetch** (so the first tick happens after ``CRL_SYNC_INTERVAL``
seconds, not immediately). That's preserved here: the initial CRL is
fetched synchronously at the end of ``register_worker``, and this forever
loop handles every subsequent tick.
"""
from __future__ import annotations

import asyncio
import logging

import anyio
from fastapi import FastAPI

from app.settings import Settings
from app.workers.register import _fetch_and_store_crl

logger = logging.getLogger("greffer")


async def crl_sync_worker(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    try:
        while True:
            # Sleep first to match legacy order — register_worker has
            # already fetched the initial CRL.
            await asyncio.sleep(settings.crl_sync_interval)
            # _fetch_and_store_crl owns its own exception handling (logs and
            # continues); no outer try/except needed here.
            await anyio.to_thread.run_sync(_fetch_and_store_crl, settings)
    except asyncio.CancelledError:
        logger.info("crl_sync cancelled")
        raise
