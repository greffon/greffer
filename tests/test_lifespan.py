"""Tests for the FastAPI lifespan — gates worker startup on workers_enabled.

Lifespan is exercised by driving the ``lifespan(app)`` async context
manager directly. Note: ``httpx.AsyncClient + ASGITransport`` does *not*
run lifespan events in the current httpx release, so going through the
HTTP client wouldn't trigger startup/shutdown.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.lifespan import lifespan
from app.main import create_app
from app.settings import Settings
from app.workers import stop_workers


@pytest.mark.asyncio
async def test_lifespan_no_tasks_when_workers_disabled(
    settings: Settings,
) -> None:
    """Default settings.workers_enabled=False → start_workers is not called."""
    assert settings.workers_enabled is False
    app = create_app(token="t", settings=settings)

    with patch("app.lifespan.start_workers") as mock_start, patch(
        "app.lifespan.stop_workers"
    ) as mock_stop:
        async with lifespan(app):
            pass

    mock_start.assert_not_called()
    mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_starts_three_workers_when_enabled(
    settings: Settings,
) -> None:
    """workers_enabled=True → three tasks started with expected names,
    cancelled on shutdown."""
    settings.workers_enabled = True  # type: ignore[misc]
    app = create_app(token="t", settings=settings)

    async def _noop_worker(_app):
        await asyncio.sleep(3600)  # sleep forever; cancellable

    # Patch the bindings that `start_workers` uses — those are module-level
    # imports in `app/workers/__init__.py`, so patching `app.workers.X`
    # reaches them before `start_workers` looks them up.
    with patch("app.workers.register_worker", new=_noop_worker), patch(
        "app.workers.monitor_worker", new=_noop_worker
    ), patch("app.workers.crl_sync_worker", new=_noop_worker):
        async with lifespan(app):
            current_names = {
                t.get_name() for t in asyncio.all_tasks() if not t.done()
            }
            assert "greffer-register" in current_names
            assert "greffer-monitor" in current_names
            assert "greffer-crl-sync" in current_names

    # After lifespan shutdown the worker tasks must be gone.
    leftover = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert "greffer-register" not in leftover
    assert "greffer-monitor" not in leftover
    assert "greffer-crl-sync" not in leftover


@pytest.mark.asyncio
async def test_stop_workers_cancels_and_awaits() -> None:
    """Unit test on the orchestration helper directly."""

    async def _sleeper():
        await asyncio.sleep(3600)

    tasks = [asyncio.create_task(_sleeper()) for _ in range(3)]
    await stop_workers(tasks)
    for t in tasks:
        assert t.done()
        assert t.cancelled()
