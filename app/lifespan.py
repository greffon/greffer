from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from app.workers import start_workers, stop_workers

logger = logging.getLogger("greffer")


def _publish_token_to_sidecar(app: FastAPI) -> None:
    """Write the active greffer token to a shared volume so the
    tunnel-sidecar can authenticate to the manager with the same
    X-GREFFON-TOKEN.

    No-op when ``settings.greffer_token_file_path`` is empty (operator
    has disabled the file handoff) or when writing fails. The sidecar
    is opt-in via the ``tunnel`` compose profile, so the file is
    harmless to write in proxy-mode deployments.

    **v1 design tradeoff** — see ``app/main.py`` for the rationale.
    Static token on a shared mount is the minimum-friction choice for
    the demo / first ship. The right end state is mTLS using the
    greffer's existing built-in-CA cert; tracked in the tunnel epic's
    follow-up section. Kill this function when that lands.
    """
    path = (app.state.settings.greffer_token_file_path or "").strip()
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(app.state.greffer_token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        logger.info("greffer_token published to %s", path)
    except OSError as exc:
        # Don't crash startup if the shared volume isn't mounted —
        # proxy-mode deployments won't have it. The sidecar is the
        # only consumer; if it can't read the token it backs off and
        # logs the auth failure.
        logger.warning(
            "could not publish greffer_token to %s: %s", path, exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan — publish the token to the sidecar volume, then
    start the three background workers if enabled.

    ``greffer_workers_enabled`` defaults to False so unit tests don't
    accidentally spawn real workers. Production sets
    ``GREFFER_WORKERS_ENABLED=true`` in docker-compose.yml.
    """
    _publish_token_to_sidecar(app)
    if not app.state.settings.greffer_workers_enabled:
        logger.info("workers disabled (GREFFER_WORKERS_ENABLED unset)")
        yield
        return
    tasks = start_workers(app)
    logger.info("started %d background workers", len(tasks))
    try:
        yield
    finally:
        await stop_workers(tasks)
