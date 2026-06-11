from __future__ import annotations

from typing import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("GREFFER_ID", "test-greffer-id")
    # Point the data volume at a per-test temp dir so token persistence
    # (``create_app`` -> ``load_or_create_token`` under ``greffon_path``)
    # never writes to the real ``/data`` and never leaks a token across tests.
    monkeypatch.setenv("GREFFON_PATH", str(tmp_path))
    return get_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(token="test-token", settings=settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
