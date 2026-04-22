"""Tests for crl_sync_worker.

Key behavior: sleep-then-fetch ordering (not fetch-then-sleep), matching
the legacy ``sync_crl`` function in ``apps/utils/greffon/base_server.py``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.main import create_app
from app.settings import Settings
from app.workers.crl import crl_sync_worker


@pytest.mark.asyncio
async def test_crl_worker_sleeps_first_then_fetches(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)

    events: list[str] = []

    async def _record_sleep(s: float) -> None:
        events.append(f"sleep({s})")
        if len([e for e in events if e.startswith("sleep")]) >= 2:
            # Cancel after the second sleep so the test ends.
            raise asyncio.CancelledError

    def _record_fetch(_settings):
        events.append("fetch")

    monkeypatch.setattr("app.workers.crl.asyncio.sleep", _record_sleep)
    # Patch the binding used by crl_sync_worker (imported at module load
    # via `from app.workers.register import _fetch_and_store_crl`).
    # Patching `app.workers.register._fetch_and_store_crl` would miss
    # this already-bound reference.
    monkeypatch.setattr("app.workers.crl._fetch_and_store_crl", _record_fetch)

    with pytest.raises(asyncio.CancelledError):
        await crl_sync_worker(app)

    # First event must be a sleep, not a fetch.
    assert events[0].startswith("sleep")
    assert "fetch" in events


@pytest.mark.asyncio
async def test_crl_worker_uses_crl_sync_interval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.crl_sync_interval = 42  # type: ignore[misc]
    app = create_app(token="t", settings=settings)

    sleeps: list[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)
        raise asyncio.CancelledError  # cancel after first sleep

    monkeypatch.setattr("app.workers.crl.asyncio.sleep", _record_sleep)

    with pytest.raises(asyncio.CancelledError):
        await crl_sync_worker(app)

    assert sleeps == [42]


@pytest.mark.asyncio
async def test_crl_worker_cancellable_during_sleep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(token="t", settings=settings)

    def _noop_fetch(_s):
        return

    monkeypatch.setattr("app.workers.crl._fetch_and_store_crl", _noop_fetch)

    task = asyncio.create_task(crl_sync_worker(app))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
