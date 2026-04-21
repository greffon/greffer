from __future__ import annotations

import secrets

from fastapi import FastAPI

from app.lifespan import lifespan
from app.logging import configure_logging
from app.routers import health
from app.settings import Settings, get_settings

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
    app.state.greffer_token = token or secrets.token_urlsafe(32)
    app.state.settings = settings
    app.include_router(health.router)
    return app
