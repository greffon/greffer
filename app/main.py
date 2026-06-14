from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from fastapi import FastAPI

from app.errors import register_exception_handlers
from app.lifespan import lifespan
from app.logging import configure_logging
from app.routers import controller, health
from app.settings import Settings, get_settings
from app.token import resolve_token

# Intentionally no module-level `app = create_app()`.
# Uvicorn uses `--factory app.main:create_app` so importing this module
# has no side effects (no token minting, no logging config).


def create_app(
    token: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    app = FastAPI(lifespan=lifespan)
    # Token resolution order:
    #   1. explicit ``token=`` kwarg — tests
    #   2. ``settings.greffer_token`` (GREFFER_TOKEN env) — explicit operator
    #      override / rotation; not the default path
    #   3. a token persisted on the data volume — the production default.
    #      ``load_or_create_token`` mints one on first boot and reuses it on
    #      every subsequent boot. This MUST be stable across restarts: the
    #      manager treats token possession as the greffer's identity, so a
    #      fresh-per-process token would make every restart-on-a-new-IP look
    #      like a hijack and get rejected (``greffer_id_claimed``). Persisting
    #      it is what makes the greffer's container IP irrelevant.
    #
    # **TODO (post-launch):** migrate sidecar→manager auth to mTLS using the
    # existing built-in CA cert (greffer already holds one); kills this
    # static-token-on-disk design and aligns with the platform's CA story.
    # See tunnel epic follow-ups.
    app.state.greffer_token = token or resolve_token(settings)
    app.state.settings = settings
    # Observability (greffer-observability epic). boot_id is per-process: the
    # manager pairs it with the heartbeat seq so a restarted greffer (seq reset
    # to 1) is not dropped as stale. started_at anchors uptime. status_map is the
    # monitor's most recent collection, reused by the heartbeat so the two timers
    # don't each sweep docker. reregister_requested lets the heartbeat ask the
    # register supervisor to re-run registration after a 403.
    app.state.boot_id = uuid4().hex
    app.state.started_at = time.monotonic()
    app.state.status_map = None
    app.state.reregister_requested = asyncio.Event()
    # Set by registration once the cert is installed; the heartbeat gates every
    # beat on it so it never POSTs (and never 403-storms / triggers a concurrent
    # re-register) before the greffer is accepted. Cleared on a heartbeat 403 so
    # beating pauses until re-registration completes.
    app.state.registered = asyncio.Event()
    app.include_router(health.router)
    app.include_router(controller.router)
    register_exception_handlers(app)
    return app
