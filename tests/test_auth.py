from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import TOKEN_HEADER, require_token
from app.main import create_app
from app.settings import Settings


def _app_with_guarded_route(token: str, settings: Settings) -> FastAPI:
    app = create_app(token=token, settings=settings)

    @app.get("/guarded", dependencies=[Depends(require_token)])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.mark.asyncio
async def test_require_token_rejects_missing_header(settings: Settings) -> None:
    app = _app_with_guarded_route("secret", settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/guarded")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_token_rejects_wrong_value(settings: Settings) -> None:
    app = _app_with_guarded_route("secret", settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/guarded", headers={TOKEN_HEADER: "nope"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_token_accepts_correct_value(settings: Settings) -> None:
    app = _app_with_guarded_route("secret", settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/guarded", headers={TOKEN_HEADER: "secret"})
    assert r.status_code == 200
    assert r.json() == {"ok": "yes"}


@pytest.mark.asyncio
async def test_require_token_rejects_length_extended_match(settings: Settings) -> None:
    app = _app_with_guarded_route("secret", settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/guarded", headers={TOKEN_HEADER: "secretextra"})
    assert r.status_code == 401


def test_create_app_mints_token_when_none_passed(settings: Settings) -> None:
    app = create_app(settings=settings)
    assert isinstance(app.state.greffer_token, str)
    assert len(app.state.greffer_token) >= 32


def test_create_app_token_is_stable_across_restarts(settings: Settings) -> None:
    """Two create_app calls with the same data volume (a process restart)
    resolve the SAME token — it's persisted, not minted per process. This is
    what lets the manager recognise a restarted greffer on a new container IP
    as the same claimant instead of rejecting it as a hijack."""
    first = create_app(settings=settings)
    second = create_app(settings=settings)
    assert first.state.greffer_token == second.state.greffer_token
    # And it's actually on disk under the data volume.
    assert (settings.greffon_path / ".greffer-token").read_text().strip() == (
        first.state.greffer_token
    )


def test_create_app_explicit_token_not_persisted(settings: Settings) -> None:
    """An explicit/env token wins and must NOT be written to the volume — only
    the auto-minted default is persisted."""
    create_app(token="explicit-token", settings=settings)
    assert not (settings.greffon_path / ".greffer-token").exists()


def test_create_app_uses_provided_token(settings: Settings) -> None:
    app = create_app(token="fixed-token", settings=settings)
    assert app.state.greffer_token == "fixed-token"


def test_create_app_mints_when_token_is_empty_string(settings: Settings) -> None:
    """Empty-string token is falsy; the ``or``-fallback in create_app mints
    a fresh random token. Guards against a silent-auth-bypass regression if
    someone changes the mint to ``if token is None`` instead of ``or``."""
    app = create_app(token="", settings=settings)
    assert app.state.greffer_token != ""
    assert len(app.state.greffer_token) >= 32
