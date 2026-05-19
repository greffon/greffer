from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_id_and_status(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    # /healthz returns {id, status} — see app/routers/health.py for why.
    # The greffer-cli's reachability self-test compares `id` against the
    # GREFFER_ID it wrote into env.env.
    assert r.json() == {"id": "test-greffer-id", "status": "ok"}


@pytest.mark.asyncio
async def test_healthz_is_unauthenticated(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_healthz_id_matches_settings(client: AsyncClient) -> None:
    """The id field MUST come from settings.greffer_id; not hardcoded, not
    derived from the request. The CLI's identity check fails closed if
    this drifts."""
    r = await client.get("/healthz")
    body = r.json()
    assert "id" in body
    assert body["id"] == "test-greffer-id"  # matches conftest fixture
