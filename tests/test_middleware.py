"""Tests for the request-ID middleware (Feature #4)."""
from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_request_id_generated_and_echoed(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) >= 16  # a generated uuid4 hex


@pytest.mark.asyncio
async def test_inbound_request_id_is_propagated(client: AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "mgr-action-42"})
    assert r.headers.get("x-request-id") == "mgr-action-42"


@pytest.mark.asyncio
async def test_each_request_gets_a_distinct_id(client: AsyncClient) -> None:
    a = (await client.get("/healthz")).headers["x-request-id"]
    b = (await client.get("/healthz")).headers["x-request-id"]
    assert a != b
