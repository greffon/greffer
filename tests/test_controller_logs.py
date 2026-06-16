"""Tests for the logs endpoint on the controller router (resource-monitoring
epic, Feature 2, logs slice): LOG_SURFACING gate, token, bad cursor, deploy."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

import apps.utils.docker.compose  # noqa: F401  (lazy submodule, mirror others)
from apps.utils.docker import instance_logs as il
from app.auth import TOKEN_HEADER
from app.main import create_app
from app.settings import Settings

_IID = str(uuid.uuid4())


async def _client(settings: Settings, surfacing: bool) -> AsyncClient:
    settings.greffer_log_surfacing_enabled = surfacing
    app = create_app(token="test-token", settings=settings)
    return AsyncClient(transport=ASGITransport(app=app),
                       base_url="http://test")


@pytest.mark.asyncio
async def test_logs_404_when_surfacing_disabled(settings) -> None:
    # Default off: 404 at the source even with a valid token.
    async with await _client(settings, surfacing=False) as ac:
        r = await ac.get(f"/api/controller/greffon/{_IID}/logs/",
                         headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 404
    assert r.json()["detail"] == "log_surfacing_disabled"


@pytest.mark.asyncio
async def test_logs_requires_token(settings) -> None:
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(f"/api/controller/greffon/{_IID}/logs/")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_logs_deploy_stream_returns_lines(settings, tmp_path) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    d = tmp_path / _IID
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "deploy.log").write_bytes(b"pulling image\ncreated\n")
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(
            f"/api/controller/greffon/{_IID}/logs/?stream=deploy",
            headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["stream"] == "deploy"
    assert [ln["msg"] for ln in body["lines"]] == ["pulling image", "created"]
    assert body["next_cursor"]


@pytest.mark.asyncio
async def test_logs_missing_instance_is_404(settings, tmp_path) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(
            f"/api/controller/greffon/{_IID}/logs/?stream=deploy",
            headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 404
    assert r.json()["detail"] == "missing_on_greffer"


@pytest.mark.asyncio
async def test_logs_bad_cursor_is_400(settings, tmp_path) -> None:
    settings.greffon_path = tmp_path  # type: ignore[misc]
    d = tmp_path / _IID
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "deploy.log").write_bytes(b"x\n")
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(
            f"/api/controller/greffon/{_IID}/logs/?stream=deploy"
            f"&since=%21%21%21bad",
            headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_cursor"


@pytest.mark.asyncio
async def test_logs_forged_cursor_is_400_not_500(settings, tmp_path) -> None:
    # A decodable-but-forged cursor (wrong field type) must be a clean 400,
    # never a 500.
    settings.greffon_path = tmp_path  # type: ignore[misc]
    d = tmp_path / _IID
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "deploy.log").write_bytes(b"x\n")
    forged = il._encode_cursor({"v": 1, "off": "abc"})
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(
            f"/api/controller/greffon/{_IID}/logs/?stream=deploy&since={forged}",
            headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 400
    assert r.json()["detail"] == "bad_cursor"


@pytest.mark.asyncio
async def test_logs_invalid_stream_is_400(settings) -> None:
    # Literal[...] on the query param rejects an unknown stream pre-handler;
    # the greffer maps the RequestValidationError to 400.
    async with await _client(settings, surfacing=True) as ac:
        r = await ac.get(
            f"/api/controller/greffon/{_IID}/logs/?stream=evil",
            headers={TOKEN_HEADER: "test-token"})
    assert r.status_code == 400
