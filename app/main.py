from __future__ import annotations

import secrets

from fastapi import FastAPI

from app.errors import register_exception_handlers
from app.lifespan import lifespan
from app.logging import configure_logging
from app.routers import controller, health
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
    # Token resolution order:
    #   1. explicit ``token=`` kwarg — tests
    #   2. ``settings.greffer_token`` (GREFFER_TOKEN env) — explicit operator
    #      override / rotation; not the default path
    #   3. fresh random — the production default; lifespan publishes this
    #      token to a shared file so the tunnel-sidecar can read it.
    #
    # Sharing the token across sibling services (sidecar) is handled by
    # ``app/lifespan.py`` writing the active token to the file the
    # ``GREFFER_TOKEN_FILE`` mount points at. **TODO (post-launch):**
    # migrate sidecar→manager auth to mTLS using the existing built-in CA
    # cert (greffer already holds one); kills this static-token-on-disk
    # design and aligns with the platform's CA story. See tunnel epic
    # follow-ups.
    app.state.greffer_token = (
        token or settings.greffer_token or secrets.token_urlsafe(32)
    )
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(controller.router)
    register_exception_handlers(app)
    return app
