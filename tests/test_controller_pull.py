"""Tests for the per-greffon pull endpoints on the controller router
(resource-monitoring epic, Feature 2): stats/disk, token gate, bad-id 422,
missing-on-greffer 404."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.auth import TOKEN_HEADER

_IID = str(uuid.uuid4())

_STATS_BODY = {
    "instance_id": _IID,
    "captured_at": "2026-06-15T14:03:22.118Z",
    "containers": [
        {"service": "web", "name": f"{_IID}_web_1", "state": "running",
         "cpu_percent": 12.4, "mem_used_bytes": 268435456,
         "mem_limit_bytes": 2147483648, "net_rx_bytes": 184320,
         "net_tx_bytes": 91022, "blk_read_bytes": 0, "blk_write_bytes": 4096},
    ],
}

_DISK_BODY = {
    "instance_id": _IID,
    "captured_at": "2026-06-15T14:05:01.880Z",
    "app_dir_bytes": 104857600,
    "volumes_bytes": 5368709120,
    "total_bytes": 5473566720,
    "volumes": [{"name": f"{_IID}_db_data", "bytes": 5368709120}],
}


@pytest.mark.asyncio
async def test_stats_endpoint_returns_digest(client: AsyncClient) -> None:
    with patch("apps.utils.docker.observe.cached_instance_stats",
               return_value=_STATS_BODY):
        r = await client.get(
            f"/api/controller/greffon/{_IID}/stats/",
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["instance_id"] == _IID
    assert body["containers"][0]["cpu_percent"] == 12.4
    # Raw daemon keys are never surfaced.
    assert "cpu_stats" not in body["containers"][0]


@pytest.mark.asyncio
async def test_disk_endpoint_returns_digest(client: AsyncClient) -> None:
    with patch("apps.utils.docker.observe.cached_instance_disk",
               return_value=_DISK_BODY):
        r = await client.get(
            f"/api/controller/greffon/{_IID}/disk/",
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 200
    assert r.json()["total_bytes"] == 5473566720


@pytest.mark.asyncio
async def test_stats_missing_instance_is_404(client: AsyncClient) -> None:
    with patch("apps.utils.docker.observe.cached_instance_stats",
               return_value=None):
        r = await client.get(
            f"/api/controller/greffon/{_IID}/stats/",
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "missing_on_greffer"


@pytest.mark.asyncio
async def test_disk_missing_instance_is_404(client: AsyncClient) -> None:
    with patch("apps.utils.docker.observe.cached_instance_disk",
               return_value=None):
        r = await client.get(
            f"/api/controller/greffon/{_IID}/disk/",
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stats_requires_token(client: AsyncClient) -> None:
    r = await client.get(f"/api/controller/greffon/{_IID}/stats/")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_disk_requires_token(client: AsyncClient) -> None:
    r = await client.get(f"/api/controller/greffon/{_IID}/disk/")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_bad_id_is_400_and_never_touches_docker(
    client: AsyncClient,
) -> None:
    # A non-UUID id is rejected by FastAPI param validation BEFORE the handler
    # body (the greffer maps RequestValidationError to 400), so no
    # enumeration/disk work runs.
    with patch("apps.utils.docker.observe.cached_instance_stats") as spy:
        r = await client.get(
            "/api/controller/greffon/not-a-uuid/stats/",
            headers={TOKEN_HEADER: "test-token"},
        )
    assert r.status_code == 400
    spy.assert_not_called()
