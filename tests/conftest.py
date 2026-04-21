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
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GREFFER_ID", "test-greffer-id")
    return get_settings()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(token="test-token", settings=settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
