"""Tests for the /readyz endpoint (greffer-observability Feature #3)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings

AUTH = {"X-GREFFON-TOKEN": "test-token"}
DOCK = "app.readiness._docker_ok"


@pytest.mark.asyncio
async def test_readyz_requires_token(client: AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_readyz_degraded_when_pending_registration(
    client: AsyncClient,
) -> None:
    # Docker reachable, no crashed workers, but registration not yet accepted:
    # 200 + degraded (a pending greffer must never read fatal).
    with patch(DOCK, return_value=True):
        r = await client.get("/readyz", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert "registration_pending" in body["reasons"]
    assert body["id"] == "test-greffer-id"


@pytest.mark.asyncio
async def test_readyz_fatal_when_docker_unreachable(client: AsyncClient) -> None:
    with patch(DOCK, return_value=False):
        r = await client.get("/readyz", headers=AUTH)
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "fatal"
    assert "docker_unreachable" in body["reasons"]


@pytest.mark.asyncio
async def test_readyz_ready_when_registered_and_healthy(
    settings: Settings,
) -> None:
    app = create_app(token="test-token", settings=settings)
    app.state.registered.set()  # simulate an accepted greffer
    with patch(DOCK, return_value=True):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get("/readyz", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["reasons"] == []
