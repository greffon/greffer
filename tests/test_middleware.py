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


@pytest.mark.asyncio
async def test_crlf_injection_request_id_is_rejected(client: AsyncClient) -> None:
    # A header-splitting payload must NOT be echoed back; the middleware falls
    # back to a generated id (security: httptools does not validate header CRLF).
    evil = "abc\r\nSet-Cookie: evil=1"
    r = await client.get("/healthz", headers={"X-Request-ID": evil})
    echoed = r.headers.get("x-request-id")
    assert "\r" not in echoed and "\n" not in echoed
    assert echoed != evil
    assert "set-cookie" not in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_overlong_request_id_is_rejected(client: AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "a" * 500})
    assert len(r.headers["x-request-id"]) <= 128
